"""Tests for process supervisor backoff policy.

Can be run directly with `py -3 test/test_supervisor.py` (self-contained runner)
or via pytest.

Note: imports take the "put the package directory (`axiomatic/`) on sys.path,
then import `_supervisor` directly" path (consistent with the other test_*.py),
and **not** `from axiomatic._supervisor import ...`. The latter only works when
the repo root is on sys.path too (which the pytest run gets from `pytest.ini`'s
`pythonpath` and conftest); running this file on its own gives
`ModuleNotFoundError: No module named 'axiomatic'`.
"""
import ast
import contextlib
import gc
import importlib.util
import io
import os
import inspect
import pathlib
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))
# The repo root must be on the path too: `other_launcher_pids` internally does
# `from axiomatic._process_control import ...`; the pytest run gets the repo root
# from `pytest.ini`'s `pythonpath` and conftest, but `py -3 test/test_supervisor.py`
# does not — without this line, those tests would be
# `ModuleNotFoundError: No module named 'axiomatic'` in standalone mode.
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _supervisor import (  # noqa: E402
    RC_ALREADY_RUNNING,
    RC_ZERO_PROGRESS,
    acquire_single_instance_lock,
    child_exit_is_fatal,
    restart_backoff,
)

PKG_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic")
REPO_ROOT = os.path.dirname(PKG_ROOT)
BOT_SCRIPT = os.path.join(PKG_ROOT, "discord_bot.py")
# The bot body's instance lock is **per-platform** and lives in `state/<platform>/`.
# Compute it via `_platform_runtime` rather than re-assembling the path here: get
# it wrong and the end-to-end test below would hold a lock nobody contends for,
# so the bot runs on normally and returns a code that is not rc=3, and the red
# looks like "the gate broke".
sys.path.insert(0, PKG_ROOT)
import _platform_runtime as _pr  # noqa: E402

BOT_LOCK_FILE = str(_pr.platform_file(
    os.path.join(REPO_ROOT, ".discord_bot.lock"),
    platform=_pr.DEFAULT_PLATFORM))
BOT_LAUNCHER = os.path.join(REPO_ROOT, "start_discord_bot.py")


def test_first_retry_waits_minimum_before_growing():
    assert restart_backoff(
        5, minimum=5, maximum=300, healthy=False
    ) == (5, 10)


def test_backoff_caps_and_healthy_run_resets():
    assert restart_backoff(
        200, minimum=5, maximum=300, healthy=False
    ) == (200, 300)
    assert restart_backoff(
        300, minimum=5, maximum=300, healthy=False
    ) == (300, 300)
    assert restart_backoff(
        300, minimum=5, maximum=300, healthy=True
    ) == (5, 5)


_INVALID_CONFIGS = [(0, 5), (-1, 5), (10, 5)]


@pytest.mark.parametrize(("minimum", "maximum"), _INVALID_CONFIGS)
def test_invalid_backoff_configuration_is_rejected(minimum, maximum):
    with pytest.raises(ValueError):
        restart_backoff(
            5, minimum=minimum, maximum=maximum, healthy=False
        )


def _lock_path(tmpdir) -> str:
    return os.path.join(str(tmpdir), "instance.lock")


def test_second_acquire_is_refused_while_the_first_still_holds(tmp_path):
    """This is exactly the "two bots running at once" situation to block."""
    path = _lock_path(tmp_path)
    first = acquire_single_instance_lock(path)
    assert first is not None and not first.degraded
    try:
        assert acquire_single_instance_lock(path) is None
    finally:
        first.release()


def test_lock_is_reusable_once_the_holder_releases(tmp_path):
    """Must be reacquirable after release — otherwise the supervisor can never
    start again after one restart."""
    path = _lock_path(tmp_path)
    first = acquire_single_instance_lock(path)
    assert first is not None
    first.release()
    second = acquire_single_instance_lock(path)
    assert second is not None and not second.degraded
    second.release()


def test_release_is_idempotent(tmp_path):
    """`finally: lock.release()` may run again after already releasing; it must
    not blow up."""
    path = _lock_path(tmp_path)
    lock = acquire_single_instance_lock(path)
    assert lock is not None
    lock.release()
    lock.release()


def test_an_unusable_lock_file_lets_the_launcher_start_anyway(tmp_path):
    """When it cannot decide, fall toward "start anyway", not toward "refuse to
    start".

    This direction is deliberate and the opposite of CLAUDE.md's conservative
    direction for PID liveness probing: getting it wrong as "refuse" makes the bot
    fail to start at all over an unrelated filesystem problem, with nobody
    noticing; getting it wrong as "allow" at worst reverts to the state before
    this lock existed. The return value must be a degraded shell and **not**
    None — None is the answer reserved for "already an instance".
    """
    path = os.path.join(str(tmp_path), "no_such_dir", "nested", "instance.lock")
    lock = acquire_single_instance_lock(path)
    assert lock is not None, "must not return None — the launcher reads that as 'already an instance'"
    assert lock.degraded is True
    lock.release()


def test_a_degraded_shell_can_still_be_released(tmp_path):
    """A `degraded` empty shell (`fd is None`) must not blow up on `release()`.

    The launcher's wrap-up path is an unconditional `finally: lock.release()`, and
    it cannot tell whether it holds a real lock or a degraded shell. If this line
    blows up, the user sees a whole traceback on wrap-up, with the real exit
    reason buried underneath.
    """
    from _supervisor import InstanceLock
    shell = InstanceLock(None, _lock_path(tmp_path), degraded=True)
    shell.release()
    shell.release()             # twice does not blow up either


def test_instance_lock_must_not_grow_a_del_method():
    """`InstanceLock` **must not** have a `__del__`. Adding one silently lets a
    second instance through.

    The lock hangs off the open file description, so "closing the fd" equals
    "releasing the lock". There is currently no `__del__`, so dropping a reference
    only leaks one fd while the lock stays held until the process ends — mutual
    exclusion still holds (behaviour pinned by
    `test_dropping_the_reference_does_not_release_the_lock`).

    This test exists because that docstring once said the opposite — "once
    collected, `__del__` closing the fd equals releasing the lock". A maintainer
    reading it, finding no `__del__` on the class, is very likely to "finish it
    off" — and finishing it off would **actually** create the defect that passage
    warns about: as soon as a caller does not store the lock in a variable (or that
    variable stops being referenced in some refactor), the lock is released
    unnoticed, a second supervisor starts, and two webrunners fight over the same
    `.chrome_profile/`.

    To reclaim the fd, call `release()`; do not hang it on the object's lifetime.
    """
    from _supervisor import InstanceLock
    assert "__del__" not in InstanceLock.__dict__, (
        "InstanceLock grew a __del__. Object collected = lock released = a second "
        "instance starts silently, with no message at all. Reclaim the fd with "
        "release(), do not hang it on the object's lifetime.")
    assert not hasattr(InstanceLock, "__del__"), (
        "InstanceLock inherited a __del__ from some base class; same consequence.")


def test_dropping_the_reference_does_not_release_the_lock(tmp_path):
    """After dropping the last reference, the lock must **still be held**.

    This is the behavioural side of the previous test, and the two complement
    each other: the attribute test states the reason (do not add `__del__`), while
    this one asks about the **result** rather than the spelling, so it catches
    another way of writing it just the same.

    (Measured, incidentally: `weakref.finalize(lock, ...)` cannot even be written
    right now — `__slots__` has no `__weakref__`, so it throws
    `TypeError: cannot create weak reference`. That is an extra accidental layer
    of protection, but **do not rely on it**: someone adding a `__weakref__` to
    `__slots__` removes it, and that looks entirely harmless. This test checks the
    final result, so it still holds even after that.)
    """
    path = _lock_path(tmp_path)
    lock = acquire_single_instance_lock(path)
    assert lock is not None
    if lock.degraded:
        lock.release()
        pytest.skip("this machine cannot get a file lock; mutual exclusion untestable")

    del lock
    gc.collect()
    gc.collect()                # a reference cycle needs a second pass to reclaim

    intruder = acquire_single_instance_lock(path)
    if intruder is not None:
        intruder.release()
    assert intruder is None, (
        "the lock was released once the reference was collected — a second "
        "instance can start now. Most likely someone gave InstanceLock a "
        "__del__ (or an equivalent finalizer).")


def test_the_already_running_rc_stays_distinguishable_from_a_crash():
    """This rc's only use is "being distinguishable from other exit paths".

    0 is a normal exit, 1 is an uncaught exception and general failure, 2 is
    CPython's own command-line error — change it to any of these and the
    supervisor misreads "already an instance" as an ordinary crash and retries as
    usual, i.e. the spinning loop this rule exists to block.
    """
    assert RC_ALREADY_RUNNING not in (0, 1, 2)
    assert child_exit_is_fatal(RC_ALREADY_RUNNING) is True
    for rc in (0, 1, 2, 137, 3221225477):
        assert child_exit_is_fatal(rc) is False


def test_the_launcher_actually_consumes_the_fatal_rc():
    """"the bot returns rc=3" alone is useless — the real failure mode is the
    supervisor retrying regardless.

    Statically check that the launcher's `main()` actually calls
    `child_exit_is_fatal`. Deleting that handling makes no existing test go red
    (the bot still exits cleanly, the backoff maths is still correct), but it would
    make the launcher respawn, every 5–300 seconds, a child doomed to be blocked
    by the same lock.
    """
    with open(BOT_LAUNCHER, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), BOT_LAUNCHER)
    main_fn = next(
        (node for node in tree.body
         if isinstance(node, ast.FunctionDef) and node.name == "main"),
        None,
    )
    assert main_fn is not None, "start_discord_bot.py has no main()"
    called = {
        node.func.id
        for node in ast.walk(main_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "child_exit_is_fatal" in called, (
        "start_discord_bot.main() does not check the child's fatal rc; a bot "
        "blocked by the lock would be treated as an ordinary crash and retried "
        "forever.")


@pytest.mark.repo_write_ok(
    "state", "state/discord", "state/discord/.discord.discord_bot.lock",
    reason="the end-to-end contract itself is 'while the production instance lock "
           "is held, running the bot directly is blocked with rc=3'; the child "
           "bot reads the production lock path hardcoded in its own module, so "
           "redirecting to a temp area cannot measure it. Opening this file is "
           "only an attempt to lock, its content is irrelevant, and it is already "
           "git-ignored.")
def test_running_the_bot_directly_is_refused_with_the_fatal_rc():
    """Running `discord_bot.py` directly, bypassing the launcher, must be blocked
    too, and exit with that rc.

    This is the only end-to-end check of the whole contract: a mismatched
    constant, a bot not wired to the lock, or the gate placed after something that
    fails first — any of them goes red here. **Verified it goes red**: remove
    `main()`'s lock check and this test gets the rc of a normally-starting bot (or
    a token error) instead of 3.
    """
    if importlib.util.find_spec("discord") is None:
        pytest.skip("this interpreter has no chat-platform library; cannot run the bot body")
    # Take the lock ourselves to create the "already an instance" situation.
    # Failing to take it (`None` specifically means already held by someone else,
    # usually a running bot) is equally valid — it is the same situation, the
    # expectation is unchanged, so run on rather than skip.
    held = acquire_single_instance_lock(BOT_LOCK_FILE)
    if held is not None and held.degraded:
        held.release()
        pytest.skip("this machine cannot get a file lock; mutual exclusion untestable")
    try:
        proc = subprocess.run(
            [sys.executable, BOT_SCRIPT],
            capture_output=True, text=True, timeout=180, cwd=REPO_ROOT,
            check=False,
            # The bot's messages are UTF-8 Chinese, and on Windows **both ends**
            # of the pipe default to the local code page (cp950). `encoding="utf-8"`
            # covers only the **decode end**; the child's **encode end** needs
            # `PYTHONIOENCODING`, or the cp950 bytes received get decoded as UTF-8
            # and `errors="replace"` quietly turns them into a run of U+FFFD.
            # (This comment used to be only half right; the encode end was added
            # 2026-09-12.)
            encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    finally:
        if held is not None:
            held.release()
    assert proc.returncode == RC_ALREADY_RUNNING, (
        f"running the bot body directly should be blocked with rc={RC_ALREADY_RUNNING}, "
        f"got rc={proc.returncode}. stderr tail: {(proc.stderr or '')[-800:]}")


# --------------------------------------------------------------------------
# DoD #5: interpreter discovery order (local `.venv` → `py -3` → `sys.executable`)
# --------------------------------------------------------------------------
# `CLAUDE.md` says this order "MUST remain intact — fresh clones depend on it",
# but before these tests **nothing was checking it**, and it is written in **two
# copies** (one per launcher), a classic pair that drifts apart. The failure mode
# is just as silent: remove the `.venv` step and the dev machine still runs (the
# system interpreter happens to have things installed too), while a fresh clone
# starts on a dependency-less interpreter and then dies during import; reverse the
# order instead and the production process quietly switches to the system
# interpreter — exactly the source of the "three dependency versions" trap.
_LAUNCHERS = ("start_discord_bot.py", "start_webrunner.py")


def _python_command_node(launcher: str) -> ast.FunctionDef:
    """The AST node of `python_command()` in the launcher (no text splitting)."""
    path = os.path.join(REPO_ROOT, launcher)
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), path)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "python_command":
            return node
    raise AssertionError(
        f"{launcher} has no `python_command()` — DoD #5's discovery order lives "
        "there; if it is renamed, update this test along with it, do not let it "
        "silently fail.")


def _discovery_order(launcher: str) -> list[str]:
    """The candidates `python_command()` returns, in **execution order**.

    Deliberately looks at the order of `return`s rather than where a string
    appears in the source: the line `venv_py = REPO_ROOT / '.venv' / …` is always
    first, so judging by text position would let you move the whole `.venv` `if`
    block after `py -3` and still pass — the order swapped with nobody noticing.
    """
    returns = [node for node in ast.walk(_python_command_node(launcher))
               if isinstance(node, ast.Return) and node.value is not None]
    order: list[str] = []
    # Sorting by line number = source order. `ast.walk` is breadth-first, so a
    # return nested in an `if` would sort after the outermost
    # `return [sys.executable]`, and the order would be entirely wrong. This
    # function is a chain of guard clauses, so source order is execution order.
    for node in sorted(returns, key=lambda n: n.lineno):
        returned = ast.unparse(node.value)
        if "venv_py" in returned:
            order.append(".venv")
        elif "'-3'" in returned or '"-3"' in returned:
            order.append("py -3")
        elif "sys.executable" in returned:
            order.append("sys.executable")
    return order


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_the_interpreter_discovery_order_is_intact(launcher):
    """All three candidates must be present, and the **execution order** must not
    change."""
    expected = [".venv", "py -3", "sys.executable"]
    actual = _discovery_order(launcher)
    assert actual == expected, (
        f"{launcher}'s `python_command()` discovery order is {actual}, "
        f"DoD #5 requires {expected}. The order is the rule itself: `.venv` must be "
        "first, or the production process quietly switches to the system "
        "interpreter (dependency versions fork on the spot); and a fresh clone "
        "without `.venv` only starts thanks to the last two steps.")


def test_both_launchers_discover_the_interpreter_the_same_way():
    """The two copies of `python_command()` must be identical verbatim — this is
    the pair that drifts apart."""
    sources = {name: ast.unparse(_python_command_node(name))
               for name in _LAUNCHERS}
    first, second = _LAUNCHERS
    assert sources[first] == sources[second], (
        f"`{first}` and `{second}`'s `python_command()` differ now. Both launchers "
        "must use the same discovery order, or the bot and webrunner run on "
        "different interpreters — dependency versions fork on the spot, and it only "
        "shows once one side's import fails."
        f"\n--- {first} ---\n{sources[first]}\n--- {second} ---\n{sources[second]}")




# ---------------------------------------------------------------------------
# Landing child-process output: both launchers must have it, one implementation
#
# Hit and fixed on the webrunner side 2026-08-23: the launcher was
# `subprocess.run(cmd)`, the child inherited the console directly, and once the
# window was closed that line was gone forever. Only that one was changed;
# `start_discord_bot.py` stayed untouched until 2026-08-30 — and the bot side is
# actually worse, since the whole of Secrecy Layer 1 rests on "send a generic
# message to Discord, write full detail to the log", and the bot even replies
# "please check the log", pointing at a file that does not exist.
# ---------------------------------------------------------------------------

_LAUNCHER_PATHS = {
    "start_discord_bot.py": os.path.join(REPO_ROOT, "start_discord_bot.py"),
    "start_webrunner.py": os.path.join(REPO_ROOT, "start_webrunner.py"),
}


def _called_names(path):
    """The names of functions this launcher calls (`f(...)` and `x.f(...)` both count)."""
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


@pytest.mark.parametrize("launcher", sorted(_LAUNCHER_PATHS))
def test_both_launchers_tee_the_child_into_a_log_file(launcher):
    """Both launchers must go through `stream_child`, and both must trim their
    own log.

    `subprocess.run(cmd)` makes the child inherit the console directly — once the
    window is closed nothing is left, and what these two print (the diverging
    fields of resume decisions, raw exceptions, the supervisor's give-up reason)
    is the only clue available afterward.
    """
    names = _called_names(_LAUNCHER_PATHS[launcher])
    assert "stream_child" in names, (
        f"{launcher} does not start the child with `stream_child`. With a bare "
        "`subprocess.run` the child inherits the console, and closing the window "
        "leaves nothing.")
    assert "trim_log" in names, (
        f"{launcher} does not call `trim_log` — an append-mode log grows without bound.")


@pytest.mark.parametrize("launcher", sorted(_LAUNCHER_PATHS))
def test_no_launcher_runs_the_child_through_subprocess_run(launcher):
    """The reverse: `subprocess.run` must no longer be used to run the supervised
    child.

    This is the original pattern, and it looks entirely normal, so only a guard
    can keep it from coming back.
    """
    with open(_LAUNCHER_PATHS[launcher], "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    hits = [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"]
    assert not hits, (
        f"{launcher}:{hits} runs the child through `subprocess.run` again — that "
        "path has no log file.")


def _run_child(tmp_path, body, **kwargs):
    """Write `body` as a script, run it with `stream_child`, return `(rc, log
    content, console)`."""
    import contextlib
    import io
    import _supervisor as sup

    script = tmp_path / "child.py"
    script.write_text(body, encoding="utf-8")
    log_path = tmp_path / "child.log"
    console = io.StringIO()
    with log_path.open("a", encoding="utf-8", buffering=1) as handle:
        with contextlib.redirect_stdout(console):
            rc = sup.stream_child([sys.executable, "-u", str(script)], handle,
                                  cwd=str(tmp_path), **kwargs)
    return rc, log_path.read_text(encoding="utf-8", errors="replace"), console.getvalue()


def test_stream_child_lands_stdout_stderr_and_rc(tmp_path):
    """stdout, stderr, and rc must all land, and the console copy must remain too.

    Merging stderr is deliberate: most diagnostics go to stderr, splitting into
    two pipes would need two pump loops, and the interleaved order is the key to
    understanding what happened.
    """
    rc, log, console = _run_child(tmp_path, (
        "import sys\n"
        "print('到 stdout 的中文')\n"
        "print('to stderr', file=sys.stderr)\n"
        "sys.exit(7)\n"))
    assert rc == 7, f"rc was not passed back: {rc}"
    assert "到 stdout 的中文" in log, log[:300]
    assert "to stderr" in log, "stderr was not merged in — most diagnostics go to stderr"
    assert "到 stdout 的中文" in console, "the console copy is gone; landing must not replace live output"


def test_the_pump_does_not_deadlock_on_a_chatty_child(tmp_path):
    """Output exceeding the pipe buffer (about 64 KB on Windows) must not
    deadlock.

    With `stdout=PIPE` given but nobody reading, the child's next print hangs
    forever — worse than having no log at all (the batch stalls entirely rather
    than dropping a log). The pump must be on its own thread.
    """
    rc, log, _console = _run_child(tmp_path, (
        "import sys\n"
        "for i in range(4000):\n"
        "    sys.stdout.write('x' * 40 + ' ' + str(i) + chr(10))\n"
        "sys.stdout.write('TAIL-MARKER' + chr(10))\n"
        "sys.exit(0)\n"))
    assert rc == 0
    assert log.count("x" * 40) == 4000, (
        f"only received {log.count('x' * 40)} / 4000 lines — it wedged once the pipe filled.")
    assert "TAIL-MARKER" in log, "the last line did not arrive"


def test_a_bad_byte_from_the_child_does_not_stop_the_pump(tmp_path):
    """When the child emits illegal bytes, the output after them must keep being
    collected.

    The source is external (the child could print anything), so the decode end
    uses `errors="replace"`; if one odd byte broke the pump, the whole rest of the
    diagnostics would vanish.
    """
    _rc, log, _console = _run_child(tmp_path, (
        "import sys\n"
        "sys.stdout.buffer.write(bytes([0xff, 0xfe, 0x41]) + b'" + chr(92) + "n')\n"
        "sys.stdout.buffer.write('壞位元組之後這行還要看得到'.encode('utf-8') + b'"
        + chr(92) + "n')\n"
        "sys.stdout.buffer.flush()\n"))
    assert "壞位元組之後這行還要看得到" in log, log[:300]


def test_the_child_gets_utf8_io_encoding(tmp_path):
    """The child is forced to UTF-8 text I/O.

    A pipe is not a console, so CPython falls back to the system locale encoding
    (cp950 here), and Chinese output can blow up **the child itself** — the case
    where adding a log file breaks things instead.

    Pop the parent's own `PYTHONIOENCODING` before measuring: otherwise this test
    is always green on a machine where "the developer's shell happens to have set
    it", measuring the environment rather than the code (this machine's shell is
    `UTF-8`).
    """
    saved = os.environ.pop("PYTHONIOENCODING", None)
    try:
        _rc, log, _console = _run_child(tmp_path, (
            "import sys" + chr(10) +
            "print('ENC=' + (sys.stdout.encoding or '?').lower())" + chr(10)))
    finally:
        if saved is not None:
            os.environ["PYTHONIOENCODING"] = saved
    assert "enc=utf-8" in log.lower(), (
        f"the child's stdout encoding is not utf-8: {log[:200]!r}")


def test_a_wrong_pythonioencoding_in_the_parent_is_overridden(tmp_path):
    """When the caller's environment carries a **wrong** `PYTHONIOENCODING`, the
    child must still emit UTF-8.

    This is the other half of the previous test, and the only half that tells
    "force" apart from "yield". The previous test does `os.environ.pop(...)` before
    measuring (which is right, and its reasoning is spelled out in its docstring),
    but for that reason only covers the "variable absent from the environment"
    case — and in that case `env.setdefault(...)` (yield) and `env[...] = ...`
    (overwrite) behave **identically**, so the "turn force into yield" mutation
    survived until 2026-09-12.

    Yield's direction happens to be the silently-broken side: `stream_child`'s
    decode end is a **hardcoded** `encoding="utf-8", errors="replace"`, so if the
    child encodes as cp950/big5, what comes back is a run of U+FFFD — no exception,
    no red text, a normal rc, and only the Traditional-Chinese progress lines,
    resume-mismatch reasons, and give-up reasons in `webrunner.log` turned into
    question marks. And the launcher's most common start paths (desktop shortcut,
    autostart, scheduled task, someone else's shell) are exactly the ones that
    carry "a value in the environment we never set".

    It measures **how the child actually encodes** rather than the env dict passed
    down: this test's target is the named guard `stream_child`, which exists
    precisely because a static scanner cannot tell it opens Python (the command
    line is a variable the caller supplies). Only an end-to-end measurement counts.
    """
    saved = os.environ.get("PYTHONIOENCODING")
    os.environ["PYTHONIOENCODING"] = "cp950"
    try:
        _rc, log, _console = _run_child(tmp_path, (
            "import sys" + chr(10) +
            "print('ENC=' + (sys.stdout.encoding or '?').lower())" + chr(10) +
            "print('繁中這一行要原樣回來')" + chr(10)))
    finally:
        if saved is None:
            os.environ.pop("PYTHONIOENCODING", None)
        else:
            os.environ["PYTHONIOENCODING"] = saved
    assert "enc=utf-8" in log.lower(), (
        "the caller's environment carried `PYTHONIOENCODING=cp950` and the child "
        "followed it — this is a yield (`setdefault`) rather than an overwrite, "
        "while the decode end is hardcoded utf-8. "
        f"The encoding the child reported: {log[:200]!r}")
    assert "繁中這一行要原樣回來" in log, (
        f"the two ends' encodings disagreed and the Chinese line did not come back verbatim: {log[:200]!r}")
    # Use `chr(0xFFFD)` rather than pasting a replacement character directly: a
    # real U+FFFD in the source reads as if this file itself broke, and the next
    # editor might "correct" it out of hand.
    assert chr(0xFFFD) not in log, (
        f"the log has {log.count(chr(0xFFFD))} replacement characters (U+FFFD), "
        "meaning the child wrote in another encoding and we decoded as utf-8.")


def test_log_lines_carry_a_timestamp_but_the_console_does_not(tmp_path):
    """The log copy carries a timestamp (must line up with `events.ndjson`'s ts
    afterward); the console copy does not."""
    import re
    _rc, log, console = _run_child(tmp_path, "print('MARK')\n")
    line = next(l for l in log.splitlines() if "MARK" in l)
    assert re.match(r"^\[\d\d-\d\d \d\d:\d\d:\d\d\] MARK", line), line
    assert console.splitlines()[0] == "MARK", (
        f"the console copy got a prefix: {console.splitlines()[0]!r}")


def test_say_goes_to_both_the_console_and_the_log(tmp_path):
    """The supervisor's own messages must land too.

    Give-up reasons, rcs, and backoff seconds are exactly what after-the-fact
    diagnosis needs to see, and printing only to the console keeps nothing.
    """
    import contextlib
    import io
    import _supervisor as sup

    log_path = tmp_path / "say.log"
    console = io.StringIO()
    with log_path.open("a", encoding="utf-8", buffering=1) as handle:
        with contextlib.redirect_stdout(console):
            sup.say(handle, "supervisor: giving up after 5 attempts")
    text = log_path.read_text(encoding="utf-8")
    assert "giving up after 5 attempts" in text
    assert "giving up after 5 attempts" in console.getvalue()


def test_say_and_log_write_tolerate_no_log_at_all():
    """When the log file cannot be opened (read-only disk, permissions), the
    launcher must still run.

    `log=None` is an explicitly supported state: if it cannot land, fall back to
    console-only, and do not refuse to start over it.
    """
    import contextlib
    import io
    import _supervisor as sup

    console = io.StringIO()
    with contextlib.redirect_stdout(console):
        sup.say(None, "still speaks")
        sup.log_write(None, "swallowed\n")
    assert "still speaks" in console.getvalue()


def test_trim_log_keeps_the_tail_and_cuts_on_a_line_boundary(tmp_path):
    """Trimming keeps the **tail** and cuts on a whole-line boundary.

    The seam that blows up is exactly "crash → respawn", so keep the last stretch
    rather than the first; cutting on a half-line makes the first line an
    unreadable fragment.
    """
    import _supervisor as sup

    path = tmp_path / "big.log"
    path.write_text("".join(f"line {i}\n" for i in range(20000)), encoding="utf-8")
    sup.trim_log(path, max_bytes=50_000, keep_bytes=20_000)
    text = path.read_text(encoding="utf-8")
    assert path.stat().st_size <= 20_000, path.stat().st_size
    assert text.splitlines()[-1] == "line 19999", "the tail was not kept"
    assert text.splitlines()[0].startswith("line "), (
        f"the first line is a half-line fragment: {text.splitlines()[0]!r}")

    # Below the limit, not a single byte should be touched.
    before = path.read_bytes()
    sup.trim_log(path, max_bytes=10_000_000, keep_bytes=1000)
    assert path.read_bytes() == before, "trimmed even though it was below the limit"


def test_trim_log_must_not_become_an_atomic_write(tmp_path):
    """`trim_log` **deliberately** overwrites in place; it must not become
    "same-directory temp → os.replace".

    This looks like it conflicts with `CLAUDE.md`'s cross-process atomic-write
    hard rule, so the reason is pinned here, or sooner or later someone "tidily
    fixes" it — and `webrunner.log` really is a cross-process file (the launcher
    writes, the bot's `/log tail` reads).

    The difference is that **this file has two live append handles at once**: the
    launcher's tee, and the third-party library's own logger (whose filename on
    Windows differs from ours only in case, i.e. **the same file** —
    `Path("webrunner.log").resolve()` points at `WEBRunner.log` on disk. Measured:
    that file really does contain lines in both formats).

    On 2026-08-30 both approaches were measured on this machine (the result is
    exactly what the block below verifies):
      * In-place overwrite: succeeds, and the other handle's **subsequent appends
        still land in the visible file**.
      * `os.replace`: straight to `PermissionError [WinError 5] access denied` —
        Windows will not let you replace a file someone else has open.

    The latter's outcome is not "one missed trim" but **never trimming again**:
    `trim_log` swallows `OSError` and just returns, so the log grows without bound
    and says nothing. An atomic write here is not safer, it is a silent failure.
    """
    import _supervisor as sup

    log = tmp_path / "app.log"
    log.write_text("old-line\n" * 400, encoding="utf-8")

    # Simulate the third-party library: hold the same file open in append mode.
    holder = open(log, "a", encoding="utf-8")
    try:
        holder.write("from-the-other-handle-1\n")
        holder.flush()

        sup.trim_log(log, max_bytes=200, keep_bytes=100)
        assert log.stat().st_size <= 200, "did not trim"

        # After trimming, the other handle still writes into the **visible** file.
        holder.write("from-the-other-handle-2\n")
        holder.flush()
        text = log.read_text(encoding="utf-8")
        assert "from-the-other-handle-2" in text, (
            "trimming swapped the inode, and the other process's append fell into "
            "the now-invisible old file. trim_log must overwrite in place.")
    finally:
        holder.close()

    # The reverse: confirm os.replace really does fail in this situation — this
    # test's rationale rests on that, so if platform behaviour ever changes this
    # goes red, and then the whole rule should be re-evaluated rather than quietly
    # left in place.
    if os.name == "nt":
        log2 = tmp_path / "app2.log"
        log2.write_text("x\n", encoding="utf-8")
        keeper = open(log2, "a", encoding="utf-8")
        try:
            tmp = tmp_path / "app2.log.tmp"
            tmp.write_bytes(b"replacement\n")
            try:
                os.replace(tmp, log2)
                raise AssertionError(
                    "Windows now allows replacing an open file — this test's "
                    "premise has changed; re-evaluate whether trim_log should "
                    "become an atomic write, do not just delete this block.")
            except PermissionError:
                pass
        finally:
            keeper.close()


def test_trim_log_does_not_use_an_atomic_writer():
    """The static side: `os.replace` must not appear in `trim_log`'s source.

    The test above verifies behaviour; this one blocks a "looks right" one-line
    change. Both are needed — the behavioural one needs platform cooperation, the
    static one blocks on any platform.
    """
    import _supervisor as sup

    source = inspect.getsource(sup.trim_log)
    assert "os.replace" not in source and "replace(" not in source, (
        "trim_log uses os.replace. This file has two live append handles at once, "
        "on Windows os.replace goes straight to PermissionError, and trim_log "
        "swallows OSError — the result is the log never being trimmed again, and "
        "entirely silently.")


def test_trim_log_never_raises_on_a_missing_file(tmp_path):
    """When the log file is missing / unreadable, just do not trim — the
    supervisor must not die over a log file."""
    import _supervisor as sup
    sup.trim_log(tmp_path / "nope.log", max_bytes=1, keep_bytes=1)


def main():
    # Parametrized cases must be expanded by hand in standalone, or some are missed.
    import tempfile

    groups = [
        ("test_first_retry_waits_minimum_before_growing",
         test_first_retry_waits_minimum_before_growing),
        ("test_backoff_caps_and_healthy_run_resets",
         test_backoff_caps_and_healthy_run_resets),
        ("test_the_already_running_rc_stays_distinguishable_from_a_crash",
         test_the_already_running_rc_stays_distinguishable_from_a_crash),
        ("test_instance_lock_must_not_grow_a_del_method",
         test_instance_lock_must_not_grow_a_del_method),
        ("test_the_contended_errno_list_covers_both_platforms",
         test_the_contended_errno_list_covers_both_platforms),
        ("test_the_launcher_actually_consumes_the_fatal_rc",
         test_the_launcher_actually_consumes_the_fatal_rc),
        ("test_running_the_bot_directly_is_refused_with_the_fatal_rc",
         test_running_the_bot_directly_is_refused_with_the_fatal_rc),
    ]
    for name, fn in [
        ("test_second_acquire_is_refused_while_the_first_still_holds",
         test_second_acquire_is_refused_while_the_first_still_holds),
        ("test_lock_is_reusable_once_the_holder_releases",
         test_lock_is_reusable_once_the_holder_releases),
        ("test_release_is_idempotent", test_release_is_idempotent),
        ("test_an_unusable_lock_file_lets_the_launcher_start_anyway",
         test_an_unusable_lock_file_lets_the_launcher_start_anyway),
        ("test_a_degraded_shell_can_still_be_released",
         test_a_degraded_shell_can_still_be_released),
        ("test_dropping_the_reference_does_not_release_the_lock",
         test_dropping_the_reference_does_not_release_the_lock),
        # The three POSIX-branch tests deliberately avoid the monkeypatch fixture,
        # so standalone reaches them too.
        ("test_the_posix_branch_takes_a_non_blocking_exclusive_flock",
         test_the_posix_branch_takes_a_non_blocking_exclusive_flock),
        ("test_the_posix_branch_maps_a_locked_file_to_none_and_closes_the_fd",
         test_the_posix_branch_maps_a_locked_file_to_none_and_closes_the_fd),
        ("test_the_posix_branch_without_fcntl_degrades_instead_of_refusing",
         test_the_posix_branch_without_fcntl_degrades_instead_of_refusing),
        ("test_an_oserror_without_an_errno_is_also_undecidable",
         test_an_oserror_without_an_errno_is_also_undecidable),
    ]:
        groups.append((
            name,
            lambda f=fn: [
                f(d) for d in [tempfile.mkdtemp(prefix="supervisor_test_")]
            ][0],
        ))
    # The two errno-whitelist groups — the core of this change, standalone must
    # reach them too.
    for errno_name, expected in _CONTENDED_ERRNOS:
        groups.append((
            "test_a_contended_lock_errno_still_means_another_instance"
            f"[{errno_name}]",
            lambda n=errno_name, e=expected: [
                test_a_contended_lock_errno_still_means_another_instance(n, e, d)
                for d in [tempfile.mkdtemp(prefix="supervisor_errno_")]
            ][0],
        ))
    for errno_name in _UNDECIDABLE_ERRNOS:
        groups.append((
            "test_an_unknown_lock_errno_degrades_instead_of_claiming_"
            f"another_instance[{errno_name}]",
            lambda n=errno_name: [
                test_an_unknown_lock_errno_degrades_instead_of_claiming_another_instance(  # noqa: E501
                    n, d)
                for d in [tempfile.mkdtemp(prefix="supervisor_errno_")]
            ][0],
        ))
    for minimum, maximum in _INVALID_CONFIGS:
        groups.append((
            f"test_invalid_backoff_configuration_is_rejected"
            f"[{minimum}-{maximum}]",
            lambda lo=minimum, hi=maximum:
                test_invalid_backoff_configuration_is_rejected(lo, hi),
        ))
    for launcher in _LAUNCHERS:
        groups.append((
            f"test_the_interpreter_discovery_order_is_intact[{launcher}]",
            lambda name=launcher:
                test_the_interpreter_discovery_order_is_intact(name),
        ))
    groups.append((
        "test_both_launchers_discover_the_interpreter_the_same_way",
        test_both_launchers_discover_the_interpreter_the_same_way))
    # DoD #5's behavioural side: these deliberately use `_Swapped` rather than the
    # monkeypatch fixture, precisely so the standalone runner reaches them too (the
    # AST guard cannot see whether the wiring is right).
    for launcher in _LAUNCHERS:
        for os_name in sorted(_VENV_LAYOUT):
            groups.append((
                "test_a_clone_with_a_venv_runs_that_exact_interpreter"
                f"[{launcher}-{os_name}]",
                lambda n=launcher, o=os_name: [
                    test_a_clone_with_a_venv_runs_that_exact_interpreter(
                        n, o, pathlib.Path(d))
                    for d in [tempfile.mkdtemp(prefix="supervisor_py_")]
                ][0]))
            groups.append((
                "test_the_other_platforms_venv_layout_is_not_accepted"
                f"[{launcher}-{os_name}]",
                lambda n=launcher, o=os_name: [
                    test_the_other_platforms_venv_layout_is_not_accepted(
                        n, o, pathlib.Path(d))
                    for d in [tempfile.mkdtemp(prefix="supervisor_py_")]
                ][0]))
        for name, fn in [
            ("test_a_fresh_clone_without_a_venv_falls_back_to_the_py_launcher",
             test_a_fresh_clone_without_a_venv_falls_back_to_the_py_launcher),
            ("test_without_a_venv_or_a_py_launcher_it_uses_the_current_interpreter",  # noqa: E501
             test_without_a_venv_or_a_py_launcher_it_uses_the_current_interpreter),
            ("test_the_venv_beats_the_py_launcher_when_both_are_available",
             test_the_venv_beats_the_py_launcher_when_both_are_available),
        ]:
            groups.append((
                f"{name}[{launcher}]",
                lambda f=fn, n=launcher: [
                    f(n, pathlib.Path(d))
                    for d in [tempfile.mkdtemp(prefix="supervisor_py_")]
                ][0]))
    groups.append((
        "test_every_copy_of_python_command_is_accounted_for",
        test_every_copy_of_python_command_is_accounted_for))
    groups.append((
        "test_the_autostart_copy_is_deliberately_different_not_a_missed_one",
        test_the_autostart_copy_is_deliberately_different_not_a_missed_one))
    groups.append((
        "test_the_python_command_registration_bites_on_a_synthetic_corpus",
        test_the_python_command_registration_bites_on_a_synthetic_corpus))
    for launcher in sorted(_LAUNCHER_PATHS):
        groups.append((
            f"test_both_launchers_tee_the_child_into_a_log_file[{launcher}]",
            lambda name=launcher:
                test_both_launchers_tee_the_child_into_a_log_file(name)))
        groups.append((
            f"test_no_launcher_runs_the_child_through_subprocess_run[{launcher}]",
            lambda name=launcher:
                test_no_launcher_runs_the_child_through_subprocess_run(name)))
    for name, fn in [
        ("test_stream_child_lands_stdout_stderr_and_rc",
         test_stream_child_lands_stdout_stderr_and_rc),
        ("test_the_pump_does_not_deadlock_on_a_chatty_child",
         test_the_pump_does_not_deadlock_on_a_chatty_child),
        ("test_a_bad_byte_from_the_child_does_not_stop_the_pump",
         test_a_bad_byte_from_the_child_does_not_stop_the_pump),
        ("test_the_child_gets_utf8_io_encoding",
         test_the_child_gets_utf8_io_encoding),
        # The other half of the same rule — only it tells overwrite apart from `setdefault`.
        ("test_a_wrong_pythonioencoding_in_the_parent_is_overridden",
         test_a_wrong_pythonioencoding_in_the_parent_is_overridden),
        ("test_log_lines_carry_a_timestamp_but_the_console_does_not",
         test_log_lines_carry_a_timestamp_but_the_console_does_not),
        ("test_say_goes_to_both_the_console_and_the_log",
         test_say_goes_to_both_the_console_and_the_log),
        ("test_trim_log_keeps_the_tail_and_cuts_on_a_line_boundary",
         test_trim_log_keeps_the_tail_and_cuts_on_a_line_boundary),
        ("test_trim_log_never_raises_on_a_missing_file",
         test_trim_log_never_raises_on_a_missing_file),
        ("test_trim_log_must_not_become_an_atomic_write",
         test_trim_log_must_not_become_an_atomic_write),
    ]:
        groups.append((
            name,
            lambda f=fn: [
                f(pathlib.Path(d))
                for d in [tempfile.mkdtemp(prefix="supervisor_log_test_")]
            ][0],
        ))
    groups.append((
        "test_say_and_log_write_tolerate_no_log_at_all",
        test_say_and_log_write_tolerate_no_log_at_all))
    groups.append((
        "test_trim_log_does_not_use_an_atomic_writer",
        test_trim_log_does_not_use_an_atomic_writer))
    for name, fn in [
        ("test_a_child_that_exits_in_time_is_not_terminated",
         test_a_child_that_exits_in_time_is_not_terminated),
        ("test_a_childs_own_exit_code_is_passed_through",
         test_a_childs_own_exit_code_is_passed_through),
        ("test_a_slow_child_gets_terminated_but_not_killed",
         test_a_slow_child_gets_terminated_but_not_killed),
        ("test_a_stuck_child_is_killed_last",
         test_a_stuck_child_is_killed_last),
        ("test_the_two_timeouts_are_not_swapped",
         test_the_two_timeouts_are_not_swapped),
        # The failure-path tests that take no fixture.
        ("test_other_launcher_pids_without_psutil_returns_empty",
         test_other_launcher_pids_without_psutil_returns_empty),
        ("test_other_launcher_pids_skips_a_process_it_cannot_parse",
         test_other_launcher_pids_skips_a_process_it_cannot_parse),
        ("test_other_launcher_pids_keeps_what_it_found_when_the_scan_blows_up",
         test_other_launcher_pids_keeps_what_it_found_when_the_scan_blows_up),
        ("test_echo_line_falls_back_when_the_console_cannot_encode",
         test_echo_line_falls_back_when_the_console_cannot_encode),
        ("test_a_broken_console_does_not_stop_the_pump",
         test_a_broken_console_does_not_stop_the_pump),
        ("test_pump_stream_tolerates_no_stream_at_all",
         test_pump_stream_tolerates_no_stream_at_all),
        ("test_pump_stream_stops_quietly_when_the_pipe_dies",
         test_pump_stream_stops_quietly_when_the_pipe_dies),
    ]:
        groups.append((name, fn))
    skipped = 0
    for name, fn in groups:
        print(name)
        try:
            fn()
        except pytest.skip.Exception as reason:  # environment limit, not a failure
            skipped += 1
            print(f"  SKIP ({reason})\n")
            continue
        print("  PASS\n")
    suffix = f" ({skipped} skipped due to environment)" if skipped else ""
    # This runner registers each test **by name individually**, so it will always
    # lag behind the tests actually defined in the file — measured 30/34 on
    # 2026-09-03, the missing four being the ones that take a pytest fixture /
    # parametrize, which it rightly cannot invoke. The problem is not running
    # fewer, but **not saying so**: an unconditional "ALL N PASSED" reads as
    # "everything passed". So print the shortfall, following `test_bot_helpers`.
    try:
        declared = sum(
            1 for node in ast.parse(
                pathlib.Path(__file__).read_text(encoding="utf-8")).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_"))
        missing = declared - len(groups)
        if missing > 0:
            suffix += (f" ({missing} more need a pytest fixture / parametrize and "
                       "standalone cannot reach them; for the full result run pytest)")
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    print(f"ALL {len(groups) - skipped} TEST GROUPS PASSED{suffix}")
    return 0



# ---------------------------------------------------------------------------
# Both launchers must have a single-instance lock
#
# Added 2026-09-03. `start_discord_bot.py` has had one since 2026-08-30;
# `start_webrunner.py` never did — purely an omission, and **the unlocked side
# has the heavier consequences**: the bot body has a second lock backing it up,
# the webrunner has not one layer. Two batch supervisors running at once ⇒ two
# webrunners fighting over the same `.chrome_profile/`, nuclear-sweeping each
# other, picking the same `todo_*.md` items twice, both looking normal. Once
# "autostart" is wired up this goes from occasional to routine (the user has one
# open manually, the startup task opens another), so the lock is a prerequisite
# for that path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_every_launcher_takes_a_single_instance_lock(launcher):
    """Use AST to check it is actually called, not just imported and left sitting.

    This test exists because of the asymmetry it caught: the two launchers look
    almost identical, a missing lock is invisible from the outside, and the
    symptom (duplicate instances) looks like something else broke.
    """
    path = os.path.join(REPO_ROOT, launcher)
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "acquire_single_instance_lock" in called, (
        f"{launcher} does not acquire a single-instance lock. Both launchers must "
        "block 'being started twice' — the webrunner side especially, since it has "
        "no second layer of protection underneath.")


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_every_launcher_locks_its_own_file(launcher):
    """One lock file per program. Sharing one means the launcher blocks the very
    child it spawned."""
    path = os.path.join(REPO_ROOT, launcher)
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    names = {node.targets[0].id: ast.unparse(node.value)
             for node in ast.walk(tree)
             if isinstance(node, ast.Assign) and len(node.targets) == 1
             and isinstance(node.targets[0], ast.Name)}
    assert "LOCK_FILE" in names, f"{launcher} does not define LOCK_FILE"


def test_the_two_launchers_do_not_share_a_lock_file():
    """Sharing one lock file = whichever starts first blocks the other
    permanently."""
    seen = {}
    for launcher in _LAUNCHERS:
        path = os.path.join(REPO_ROOT, launcher)
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "LOCK_FILE"):
                seen[launcher] = ast.unparse(node.value)
    assert len(seen) == len(_LAUNCHERS), f"a launcher has no LOCK_FILE: {seen}"
    assert len(set(seen.values())) == len(seen), (
        f"the two launchers share the same lock file: {seen}. That makes one of "
        "them unable to start forever.")
    # The bot body's own lock must not be shared with the launcher either (see
    # acquire_single_instance_lock).
    assert ".discord_bot.lock" not in {v.split('/')[-1].strip('\'"')
                                       for v in seen.values()}


def test_other_launcher_pids_ignores_a_process_that_merely_mentions_the_name():
    """A substring match would count a shell or `python -c` that "just mentions
    the filename on the command line" as an instance.

    Measured on the `_process_control` side, 5 of 7 entries were this kind; after
    lifting it up to be shared, make sure it was not lost in the move.
    """
    from _supervisor import other_launcher_pids
    procs = [(11, "python.exe", ["python.exe", "-c", "print('start_webrunner.py')"], 1)]
    assert other_launcher_pids("start_webrunner.py", self_pid=99, procs=procs) == []


def test_other_launcher_pids_finds_a_real_second_instance():
    from _supervisor import other_launcher_pids
    procs = [(11, "python.exe",
              ["C:/py/python.exe", "D:/Work/Example/start_webrunner.py"], 1)]
    assert other_launcher_pids("start_webrunner.py", self_pid=99, procs=procs) == [11]


def test_other_launcher_pids_excludes_itself():
    """We are not "another instance"."""
    from _supervisor import other_launcher_pids
    procs = [(99, "python.exe",
              ["C:/py/python.exe", "D:/Work/Example/start_webrunner.py"], 1)]
    assert other_launcher_pids("start_webrunner.py", self_pid=99, procs=procs) == []


def test_other_launcher_pids_collapses_the_shim_and_the_real_interpreter():
    r"""`.venv\Scripts\python.exe` is a stub that spawns the real interpreter:
    identical cmdline, parent/child relationship. If not collapsed, one existing
    instance gets reported as two pids and the reader thinks they opened two."""
    from _supervisor import other_launcher_pids
    cmd = ["D:/Work/Example/.venv/Scripts/python.exe",
           "D:/Work/Example/start_webrunner.py"]
    procs = [(11, "python.exe", cmd, 1),      # the stub
             (12, "python.exe", cmd, 11)]     # the real one (parent = 11)
    assert other_launcher_pids("start_webrunner.py", self_pid=99,
                               procs=procs) == [11]


def test_other_launcher_pids_can_be_narrowed_to_one_platform():
    """The bot's launcher is **one process per platform**, and they share one
    script filename.

    Without narrowing, "is the telegram one running" gets answered "yes" by
    another platform's process, so `wake_autostart` can never wake it — and the
    symptom is "wake complete", which looks entirely normal.
    """
    from _supervisor import other_launcher_pids
    script = "start_discord_bot.py"
    procs = [(11, "python.exe",
              ["C:/py/python.exe", f"D:/x/{script}", "--platform", "discord"], 1),
             (12, "python.exe",
              ["C:/py/python.exe", f"D:/x/{script}", "--platform", "telegram"], 1)]
    assert other_launcher_pids(script, self_pid=99, procs=procs) == [11, 12]
    assert other_launcher_pids(script, self_pid=99, procs=procs,
                               also_contains="telegram") == [12]
    assert other_launcher_pids(script, self_pid=99, procs=procs,
                               also_contains="discord") == [11]


def test_the_platform_filter_matches_a_whole_argument_not_a_substring():
    """`telegram2` is not `telegram`. Match a whole argv element verbatim, not a
    substring."""
    from _supervisor import other_launcher_pids
    script = "start_discord_bot.py"
    procs = [(11, "python.exe",
              ["C:/py/python.exe", f"D:/x/{script}", "--platform", "telegram2"], 1)]
    assert other_launcher_pids(script, self_pid=99, procs=procs,
                               also_contains="telegram") == []


def test_other_launcher_pids_never_raises_on_junk():
    """A diagnostic must not crash the launcher."""
    from _supervisor import other_launcher_pids
    for junk in ([(None, None, None, None)], [(1, "python.exe", None, 0)], []):
        assert other_launcher_pids("start_webrunner.py", self_pid=9,
                                   procs=junk) == []


def _load_launcher(name):
    """Load the launcher as a module (without running `main()`)."""
    path = os.path.join(REPO_ROOT, name)
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_a_held_lock_stops_the_launcher_before_it_spawns_anything(
        launcher, tmp_path, monkeypatch):
    """When the lock is held by someone else, the launcher must exit **before**
    spawning anything.

    The two AST tests only prove "it is called, the lock files differ", not
    whether the wiring is right — `if lock is None` written backwards passes just
    the same. This one enters through `main()` and actually runs, asserting "the
    supervise loop was never called at all", i.e. the very fact that "no second
    browser stack was opened".

    It deliberately does **not** run the real launcher in a subprocess: if the
    lock failed to block, a webrunner really would be spawned, and a webrunner on
    startup unconditionally nuclear-sweeps all chrome — the production batch's
    browsers would be killed on the spot. Testing a safety mechanism should not
    risk the very thing it guards against.
    """
    module = _load_launcher(launcher)
    monkeypatch.setattr(module, "LOCK_FILE", tmp_path / "test.lock")
    monkeypatch.setattr(module, "WEBRUNNER_LOG" if "webrunner" in launcher
                        else "BOT_LOG", tmp_path / "test.log")
    monkeypatch.setattr(sys, "argv", [launcher])

    # The interceptor **raises** rather than returning an rc. If it returned an rc,
    # the bot's `while True:` supervise loop would treat "the child exited cleanly"
    # as a cue to respawn, spinning once every 5 seconds forever — which is exactly
    # how this test hung under mutation testing (hit for real). A hung test is
    # worse than a red one: red points at the problem, hung just leaves the whole
    # run stuck there.
    class _Spawned(Exception):
        pass

    for hook in ("_supervise", "stream_child"):
        if hasattr(module, hook):
            def _boom(*_a, **_k):
                raise _Spawned
            monkeypatch.setattr(module, hook, _boom)

    held = acquire_single_instance_lock(tmp_path / "test.lock")
    if held is None or held.degraded:
        if held is not None:
            held.release()
        pytest.skip("this machine cannot get a file lock; mutual exclusion untestable")
    try:
        try:
            rc = module.main()
        except _Spawned:
            pytest.fail(
                f"{launcher} **still spawned** while the lock was held. This is "
                "exactly what this lock blocks: two stacks running at once, both "
                "looking normal.")
    finally:
        held.release()

    assert rc != 0, f"{launcher} was blocked by the lock but reported success (rc={rc})"


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_a_missing_target_script_stops_the_launcher_before_anything_else(
        launcher, tmp_path, monkeypatch, capsys):
    """When the program to supervise is missing (moved, renamed, incomplete
    clone): return non-zero, say a word, and stop **before taking the lock,
    trimming the log, or spawning**. Finding out only after taking the lock would
    make another healthy launcher think someone is already running. Every
    subsequent step is replaced with a trip-wire that raises."""
    module = _load_launcher(launcher)
    missing = tmp_path / "gone.py"
    if "webrunner" in launcher:
        monkeypatch.setattr(module, "SCRIPTS", {k: missing for k in module.SCRIPTS})
    else:
        monkeypatch.setattr(module, "BOT_SCRIPT", missing)
    monkeypatch.setattr(sys, "argv", [launcher])

    class _Reached(Exception):
        pass

    def _boom(*_a, **_k):
        raise _Reached

    hooks = [h for h in ("_supervise", "stream_child", "trim_log",
                         "acquire_single_instance_lock", "load_bot_config")
             if hasattr(module, h)]
    assert "acquire_single_instance_lock" in hooks, hooks
    for hook in hooks:
        monkeypatch.setattr(module, hook, _boom)
    try:
        rc = module.main()
    except _Reached:
        pytest.fail(f"{launcher} kept going even though the target program is missing")
    assert rc == 1
    assert "gone.py" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Reaping after Ctrl+C (`reap_child`)
#
# Measuring coverage on 2026-09-05 found that **not one line of this had ever
# run**. It is the "courtesy first, force after" path: wait → terminate → kill.
# Getting it wrong reports no error on the spot but leaves an orphan child — on
# the webrunner side a whole Chrome tree along with it, and the next start becomes
# two stacks fighting over the same `.chrome_profile/`. Verify with a fake proc,
# not a real process: what to pin here is the **order and timeout values**, not
# the OS's behaviour.
# ---------------------------------------------------------------------------

class _FakeProc:
    """`wait()` responds by script, one call at a time: `"timeout"` throws
    TimeoutExpired, a number is returned as the rc."""

    def __init__(self, script):
        self._script = list(script)
        self.waits = []          # the timeout each wait received (None = not given)
        self.calls = []          # the order of terminate / kill / wait

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.calls.append("wait")
        step = self._script.pop(0)
        if step == "timeout":
            raise subprocess.TimeoutExpired(cmd="child", timeout=timeout)
        return step

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")


def test_a_child_that_exits_in_time_is_not_terminated():
    import _supervisor as sup

    proc = _FakeProc([0])
    rc = sup.reap_child(proc, None, grace_sec=30.0, kill_sec=10.0)
    assert rc == 0
    assert proc.calls == ["wait"], (
        "the child finished on its own but was still terminated/killed: " + repr(proc.calls))


def test_a_childs_own_exit_code_is_passed_through():
    """Reaping must not swallow the rc — the supervisor needs it to tell "stopped
    itself" from "crashed"."""
    import _supervisor as sup

    proc = _FakeProc([3])
    assert sup.reap_child(proc, None, grace_sec=1.0, kill_sec=1.0) == 3


def test_a_slow_child_gets_terminated_but_not_killed():
    import _supervisor as sup

    proc = _FakeProc(["timeout", 0])
    rc = sup.reap_child(proc, None, grace_sec=30.0, kill_sec=10.0)
    assert rc == 0
    assert proc.calls == ["wait", "terminate", "wait"], repr(proc.calls)
    assert "kill" not in proc.calls, (
        "it finished after terminate, so there should be no kill — kill equals "
        "TerminateProcess, and the child's finally does not run at all")


def test_a_stuck_child_is_killed_last():
    import _supervisor as sup

    proc = _FakeProc(["timeout", "timeout", -9])
    rc = sup.reap_child(proc, None, grace_sec=30.0, kill_sec=10.0)
    assert rc == -9
    assert proc.calls == ["wait", "terminate", "wait", "kill", "wait"], (
        repr(proc.calls))


def test_the_two_timeouts_are_not_swapped():
    """The grace period goes to the first wait, the kill timeout to the second,
    and the last one has **no timeout**.

    Swapping them has no symptom — both are positive, the flow runs the same — but
    the meaning inverts entirely: the 30 seconds meant to let the child wrap up
    becomes only 10. If the last one also carried a timeout it would be worse:
    after kill it could throw TimeoutExpired outward, turning reaping into raising.
    """
    import _supervisor as sup

    proc = _FakeProc(["timeout", "timeout", 0])
    sup.reap_child(proc, None, grace_sec=30.0, kill_sec=10.0)
    assert proc.waits == [30.0, 10.0, None], repr(proc.waits)


# ===========================================================================
# Failure paths (filled in 2026-09-07)
#
# Measuring coverage (whole suite 2742 passed) without looking at percentages,
# only asking "which `except` branches never ran a single line", the answer was:
# **not one error handler in this module had**. The supervisor's only reason to
# exist is to "hold up when other things break", so this means its most central
# responsibility had never been verified. And it is the root of the whole
# recovery chain — if it dies on its own error-handling path, everything above it
# stops together, **and with no error message at all**, because the thing that
# would print the message is the thing that died.
#
# Filling it in caught two real defects, each with a matching behavioural test
# (not a static scan):
#   * `echo_line`'s fallback write was under no protection at all (see
#     `test_a_console_that_breaks_during_the_fallback_never_escapes`).
#   * `stream_child`'s spawn-hook failure left an orphan child (see
#     `test_a_failing_spawn_hook_does_not_leave_an_orphan`).
# ===========================================================================


class _NoModule:
    """A context manager that makes `import <name>` really throw `ImportError`.

    **You must write `sys.modules[name] = None`, never `pop` / `del`.** `pop` only
    clears the cache, and the next `import` reloads **the real one** from disk —
    that is how, on 2026-09-07, a test "simulating psutil being absent" got the
    real psutil and then `proc.kill()` killed the Chrome of a 78.7-hour production
    batch on this machine, while the test on the surface only failed an assertion.
    When CPython sees `sys.modules[name] is None` it throws `ImportError` outright,
    without looking for the file — that is what "absent" means.
    """

    _ABSENT = object()

    def __init__(self, *names):
        self._names = names
        self._saved = {}

    def __enter__(self):
        for name in self._names:
            self._saved[name] = sys.modules.get(name, self._ABSENT)
            sys.modules[name] = None
        return self

    def __exit__(self, *_exc):
        for name, saved in self._saved.items():
            if saved is self._ABSENT:
                sys.modules.pop(name, None)   # was absent to begin with; restore = remove
            else:
                sys.modules[name] = saved
        return False


# ---------------------------------------------------------------------------
# Single-instance lock: verify mutual exclusion with **two real processes**
# ---------------------------------------------------------------------------
#
# The existing test (`test_second_acquire_is_refused_while_the_first_still_holds`)
# opens two fds in the same process. That proves "one program cannot lock itself
# twice", not the situation this lock really guards — **the autostart copy and
# the user's own copy are two processes**. Windows' `msvcrt.locking` and POSIX's
# `flock` both hang off the open file description, and same-process / cross-process
# semantics can differ, so it only counts as verified once it is cross-process.
#
# Lock files are always opened in `tmp_path`: do not so much as touch the
# production `.webrunner_supervisor.lock` / `.discord_bot_supervisor.lock`, since
# this machine has a long-running production batch.

_LOCK_PROBE_SOURCE = '''
"""One-shot probe: try to acquire the single-instance lock, write the result to a verdict file."""
import os
import pathlib
import sys
import time

sys.path.insert(0, sys.argv[1])
from _supervisor import acquire_single_instance_lock

lock_path, verdict_path, go_path, mode = sys.argv[2:6]
lock = acquire_single_instance_lock(lock_path)
if lock is None:
    verdict = "REFUSED"
elif lock.degraded:
    verdict = "DEGRADED"
else:
    verdict = "ACQUIRED"
tmp = verdict_path + ".tmp"
pathlib.Path(tmp).write_text(verdict, encoding="utf-8")
os.replace(tmp, verdict_path)

if mode == "hold" and verdict == "ACQUIRED":
    # Wait for the parent to release. 60 seconds is a **self-protection cap**: if
    # the test is cut down midway, no orphan is left holding the lock.
    deadline = time.time() + 60.0
    while time.time() < deadline and not pathlib.Path(go_path).exists():
        time.sleep(0.02)

if lock is not None:
    lock.release()
'''


def _spawn_lock_probe(script, tmp_path, lock_file, tag, *, mode="try"):
    """Start a real process to contend for `lock_file`, return `(proc, verdict
    file)`."""
    verdict = tmp_path / (tag + ".verdict")
    proc = subprocess.Popen(
        [sys.executable, str(script), PKG_ROOT, str(lock_file), str(verdict),
         str(tmp_path / "go"), mode],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # The child's diagnostics are UTF-8, but **both ends** of the pipe default
        # to cp950 on Windows. `encoding="utf-8"` covers only the decode end; the
        # child's encode end needs `PYTHONIOENCODING` — `_supervisor.stream_child`
        # has done this from the start, and this matches it.
        text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return proc, verdict


def _read_verdict(path, *, seconds=60.0):
    """Wait for the verdict file to appear and return its content; on timeout
    return None (**bounded**, never wedges the whole run)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        time.sleep(0.02)
    return None


def test_a_second_process_really_cannot_take_the_lock(tmp_path):
    """Two **real processes** want this lock at once, only one gets it; another
    only gets a turn after the holder exits.

    This is exactly why `.webrunner_supervisor.lock` was added on 2026-09-03: two
    batch supervisors running at once ⇒ two webrunners fighting over the same
    `.chrome_profile/`, each nuclear-sweeping the other's Chrome, picking the same
    `todo_*.md` items twice, while **both logs look normal**.

    Designing it as "the holder gets into place first, then the challenger starts"
    rather than "three grabbing at once" avoids a timing race: with a true
    simultaneous start, the winner might release before the loser even begins, so
    occasionally both are ACQUIRED — a guard that goes red at random, which
    eventually gets switched off as noise.
    """
    script = tmp_path / "lock_probe.py"
    script.write_text(_LOCK_PROBE_SOURCE, encoding="utf-8")
    lock_file = tmp_path / "cross_process.lock"
    go = tmp_path / "go"

    holder, holder_verdict = _spawn_lock_probe(
        script, tmp_path, lock_file, "holder", mode="hold")
    try:
        first = _read_verdict(holder_verdict)
        if first == "DEGRADED":
            pytest.skip("this machine cannot get a file lock; mutual exclusion untestable")
        assert first == "ACQUIRED", (
            f"the holder process did not get the lock (verdict={first!r})")

        for tag in ("other1", "other2"):
            proc, verdict_path = _spawn_lock_probe(
                script, tmp_path, lock_file, tag)
            output = proc.communicate(timeout=120)[0]
            assert proc.returncode == 0, f"the probe itself broke: {output}"
            assert _read_verdict(verdict_path, seconds=10) == "REFUSED", (
                f"{tag}: the lock is already held by another process but a second "
                "instance got it. This lock blocks exactly 'the autostart copy + "
                "the user's own copy' running at once.")
    finally:
        go.write_text("go", encoding="utf-8")
        try:
            holder.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.communicate()

    # Holder exits ⇒ the OS releases the lock immediately, and the next process
    # must be able to get it. If it cannot, a lock nobody can break was left
    # behind, and autostart can never start again.
    proc, verdict_path = _spawn_lock_probe(script, tmp_path, lock_file, "after")
    output = proc.communicate(timeout=120)[0]
    assert proc.returncode == 0, f"the probe itself broke: {output}"
    assert _read_verdict(verdict_path, seconds=10) == "ACQUIRED", (
        "the holder has already exited but the lock still cannot be obtained — a "
        "stale lock would leave the launcher unable to start again.")


def test_a_platform_without_file_locking_still_starts(tmp_path):
    """With no `msvcrt` / `fcntl`, fall toward "start anyway", and **must not**
    return None.

    Getting it wrong as "refuse" makes the launcher fail to start at all over a
    platform problem unrelated to it, with nobody noticing; getting it wrong as
    "allow" at worst reverts to the state before this lock existed. `None` is the
    answer reserved for "already an instance", so returning it on this path is a
    lie.
    """
    path = _lock_path(tmp_path)
    with _NoModule("msvcrt", "fcntl"):
        lock = acquire_single_instance_lock(path)
        assert lock is not None, "must not return None — the launcher reads that as 'already an instance'"
        assert lock.degraded is True, "when the locking mechanism is unavailable it must honestly mark itself degraded"
        # degraded is a shell with "no mutual-exclusion guarantee", so a second
        # one gets it too. This is a deliberate trade-off, written here so the
        # next reader knows it is not an oversight.
        second = acquire_single_instance_lock(path)
        assert second is not None and second.degraded is True
        second.release()
    lock.release()


def test_refusing_a_second_instance_survives_a_failing_close(tmp_path):
    """Even when `os.close` fails on the stand-aside path, it must still return
    `None` cleanly.

    This is the only exit for "another instance already exists". It does
    `os.close(fd)` on wrap-up, and close can fail (the fd was reclaimed by
    something else, a network drive throws EIO). Uncaught, the second instance
    does not print "another instance is already running" but spits a whole
    traceback — the user sees "the launcher broke", not "a second one should never
    have been opened".
    """
    path = _lock_path(tmp_path)
    first = acquire_single_instance_lock(path)
    assert first is not None
    if first.degraded:
        first.release()
        pytest.skip("this machine cannot get a file lock; mutual exclusion untestable")

    real_open, real_close = os.open, os.close
    ours: set[int] = set()

    def _fake_open(file, flags, mode=0o777, **kwargs):
        fd = real_open(file, flags, mode, **kwargs)
        if str(file) == str(path):
            ours.add(fd)
        return fd

    def _fake_close(fd):
        if fd in ours:
            ours.discard(fd)
            real_close(fd)      # really close it, or this test itself leaks the fd
            raise OSError(9, "Bad file descriptor")
        return real_close(fd)

    os.open, os.close = _fake_open, _fake_close
    try:
        second = acquire_single_instance_lock(path)
    finally:
        os.open, os.close = real_open, real_close
        first.release()
    assert second is None, (
        "the second instance should quietly get None; a close failure on wrap-up "
        "must not make it blow up with an exception.")


def test_release_survives_a_close_that_fails(tmp_path):
    """`finally: lock.release()` must not throw even when the fd is already gone.

    `release`'s note says it is just a courtesy wrap-up (the OS releases on
    process exit anyway), so it must **all the more** not be the one line that
    blows up on the launcher's wrap-up path — that would mask the real exit reason.
    """
    path = _lock_path(tmp_path)
    lock = acquire_single_instance_lock(path)
    assert lock is not None
    # Close the fd behind its back, so the close inside `release` then gets EBADF.
    os.close(lock._fd)          # noqa: SLF001  # pylint: disable=protected-access
    lock.release()              # must not throw
    again = acquire_single_instance_lock(path)
    assert again is not None, "the fd is closed, so the lock must really be released"
    again.release()


# ---------------------------------------------------------------------------
# The POSIX `fcntl.flock` branch — testable on Windows all the same (2026-09-08)
# ---------------------------------------------------------------------------
#
# A todo once said this branch "can never be reached on an ordinary machine, and
# marking it done needs a Linux box". **The premise was wrong.** The kernel's real
# flock semantics are not ours to test anyway; the stretch we wrote ourselves is
# all pure Python, and swapping the two dependencies `os.name` and `fcntl` runs it
# to completion:
#
#   * whether the right branch is taken (the `os.name` check);
#   * whether the flag is `LOCK_EX | LOCK_NB` — missing `LOCK_NB` turns it into
#     the **blocking** version, and the second instance, instead of printing
#     "another instance is already running" and exiting, quietly waits there
#     forever, with the autostart copy hung like that;
#   * whether the fd handed to flock is the one from `os.open`;
#   * whether `OSError` maps to `None` and closes the fd;
#   * whether `ImportError` degrades to a shell and **not** `None`.
#
# The lesson is worth more than the tests themselves: "this branch is only
# reachable on another OS" sounds like a fact, but is really just not yet having
# thought of how to substitute that OS condition away — the same shape as
# CLAUDE.md's "do not use a non-existent limitation as a reason to push back a
# change".

try:
    import msvcrt as _REAL_MSVCRT
except ImportError:             # non-Windows
    _REAL_MSVCRT = None

_MISSING = object()


class _PosixOs:
    """Make the `os.name` seen by `_supervisor` become `"posix"`, delegating
    everything else to the real `os`.

    Deliberately does **not** write `setattr(os, "name", "posix")`: that would
    change the `os.name` the whole process sees, and this file still has daemon
    pump threads running that should not be fooled along with it. What is swapped
    is `_supervisor`'s own `os` name binding, scoped exactly to the code under
    test.
    """

    name = "posix"

    def __getattr__(self, attr):
        return getattr(os, attr)


class _RecordingPosixOs(_PosixOs):
    """Plus bookkeeping: which fds are opened, which are closed.

    Testing "was the fd closed" without reaching into a private field like
    `lock._fd` — that would tie the test to the implementation's field name;
    recording `os.close` calls asks about the behaviour itself.
    """

    def __init__(self):
        self.opened: list[int] = []
        self.closed: list[int] = []

    def open(self, file, flags, mode=0o777, **kwargs):
        fd = os.open(file, flags, mode, **kwargs)
        self.opened.append(fd)
        return fd

    def close(self, fd):
        self.closed.append(fd)
        return os.close(fd)


class _FakeFcntl:
    """A fake `fcntl`: record the `flock` call, do nothing else.

    The constants use POSIX's real values (`LOCK_EX=2` / `LOCK_NB=4`), so a
    "pass only `LOCK_EX`" blocking-version regression gets 2 rather than 6 —
    distinguishable.
    """

    LOCK_SH = 1
    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8

    def __init__(self, error: OSError | None = None):
        self.calls: list[tuple[int, int]] = []
        self._error = error

    def flock(self, fd, operation):
        self.calls.append((fd, operation))
        if self._error is not None:
            raise self._error


def _msvcrt_must_not_be_used(*_args, **_kwargs):
    raise AssertionError(
        "the Windows branch was taken — the `os.name` check broke, and the POSIX "
        "half was never tested at all.")


class _Swapped:
    """A `setattr`-based try/finally. **Deliberately not the `monkeypatch`
    fixture**: this file's standalone runner (`py -3 test/test_supervisor.py`) has
    no pytest fixtures, and with a fixture these tests would vanish entirely on
    that path.
    """

    def __init__(self, obj, attr, value):
        self._obj, self._attr, self._value = obj, attr, value
        self._saved = _MISSING

    def __enter__(self):
        self._saved = getattr(self._obj, self._attr, _MISSING)
        setattr(self._obj, self._attr, self._value)
        return self

    def __exit__(self, *_exc):
        if self._saved is _MISSING:
            delattr(self._obj, self._attr)
        else:
            setattr(self._obj, self._attr, self._saved)
        return False


class _FakeModule:
    """Temporarily replace `sys.modules[name]` with a fake module (removed on
    restore if it was absent to begin with).

    "The module is **absent**" does not take this path but `_NoModule` —
    `sys.modules[name] = None` is the real ImportError, whereas `pop` / `del` only
    makes the next import load **the real one**.
    """

    def __init__(self, name, module):
        self._name, self._value = name, module
        self._saved = _MISSING

    def __enter__(self):
        self._saved = sys.modules.get(self._name, _MISSING)
        sys.modules[self._name] = self._value
        return self

    def __exit__(self, *_exc):
        if self._saved is _MISSING:
            sys.modules.pop(self._name, None)
        else:
            sys.modules[self._name] = self._saved
        return False


@contextlib.contextmanager
def _posix_branch(fake_fcntl):
    """Pull `_supervisor` temporarily into the POSIX branch, yielding the
    bookkeeping fake `os`.

    `fake_fcntl=None` = make `import fcntl` throw `ImportError` (the degrade path).
    Also swap `msvcrt.locking` for "blow up the moment it is called", so that if
    the branch check breaks and falls back to the Windows half, it **fails on the
    spot** rather than being inferred indirectly from "flock was not called".
    """
    import _supervisor as sup

    fake_os = _RecordingPosixOs()
    with contextlib.ExitStack() as stack:
        stack.enter_context(_Swapped(sup, "os", fake_os))
        if fake_fcntl is None:
            stack.enter_context(_NoModule("fcntl"))
        else:
            stack.enter_context(_FakeModule("fcntl", fake_fcntl))
        if _REAL_MSVCRT is not None:
            stack.enter_context(
                _Swapped(_REAL_MSVCRT, "locking", _msvcrt_must_not_be_used))
        yield fake_os


def test_the_posix_branch_takes_a_non_blocking_exclusive_flock(tmp_path):
    """The POSIX path: `flock(fd, LOCK_EX | LOCK_NB)`, and the fd is the one just
    `os.open`ed.

    `LOCK_NB` is the only truly fatal flag here. Remove it and `flock` **blocks**,
    so the second instance neither returns `None` nor prints anything — it just
    hangs there waiting for the first to finish. With the autostart copy stuck
    like that, the user only sees "the bot did not come up", with no clue.
    """
    fake_fcntl = _FakeFcntl()
    with _posix_branch(fake_fcntl) as fake_os:
        lock = acquire_single_instance_lock(_lock_path(tmp_path))
        try:
            assert lock is not None and not lock.degraded, (
                "when the POSIX branch acquires the lock it must return a normal InstanceLock")
            assert len(fake_os.opened) == 1, "should open only one fd"
            assert fake_fcntl.calls == [
                (fake_os.opened[0],
                 _FakeFcntl.LOCK_EX | _FakeFcntl.LOCK_NB)
            ], (
                "flock must take the fd os.open returned, and the flag must be "
                "LOCK_EX|LOCK_NB. Missing LOCK_NB is the blocking version: the "
                "second instance is not refused, it hangs quietly.")
        finally:
            lock.release()


def test_the_posix_branch_maps_a_locked_file_to_none_and_closes_the_fd(tmp_path):
    """Already locked by another process ⇒ return `None`, and **close the fd**.

    `None` is the answer reserved for "already an instance"; returning a degraded
    shell lets the second instance start as usual. If the fd is not closed, a
    long-lived launcher leaks one fd on every stand-aside.
    """
    fake_fcntl = _FakeFcntl(error=OSError(11, "Resource temporarily unavailable"))
    with _posix_branch(fake_fcntl) as fake_os:
        lock = acquire_single_instance_lock(_lock_path(tmp_path))
        assert lock is None, (
            "flock throwing OSError = another instance exists, must return None. "
            "Returning an InstanceLock (including a degraded shell) lets a second "
            "supervisor in.")
        assert len(fake_os.opened) == 1
        fd = fake_os.opened[0]
        assert fake_os.closed == [fd], "the stand-aside path must close the fd just opened"
        with pytest.raises(OSError):
            os.fstat(fd)        # really closed, not just recorded


def test_the_posix_branch_without_fcntl_degrades_instead_of_refusing(tmp_path):
    """When there is no `fcntl` on POSIX (extremely rare), fall toward "start
    anyway", and **must not** return `None`.

    The same rule as `test_a_platform_without_file_locking_still_starts`, but via
    **a different line of code**: that one hits `import msvcrt` on this machine,
    this one hits `import fcntl`.
    """
    with _posix_branch(None) as fake_os:
        lock = acquire_single_instance_lock(_lock_path(tmp_path))
        assert lock is not None, "must not return None — the launcher reads that as 'already an instance'"
        assert lock.degraded is True, "when the locking mechanism is unavailable it must honestly mark itself degraded"
        # This path deliberately **keeps** the fd (`InstanceLock(fd, ...)`), unlike
        # the stand-aside path which closes it on the spot.
        assert len(fake_os.opened) == 1 and fake_os.closed == []
        lock.release()
        assert fake_os.closed == [fake_os.opened[0]]


# ---------------------------------------------------------------------------
# "someone holds it" and "cannot lock here" are two different things, told apart
# by errno (2026-09-08)
# ---------------------------------------------------------------------------
#
# Before the change, `acquire_single_instance_lock`'s `except OSError` always
# `return None`, i.e. reported **every** lock failure as "another instance is
# already running". That is **exactly the opposite** of the policy this function's
# own docstring spells out (when it cannot decide, fall toward "start anyway"),
# and the failure mode is very hard to trace: a filesystem problem entirely
# unrelated to concurrency (EBADF / EINVAL / the ENOLCK a network drive that does
# not support file locks gives) makes the launcher **refuse to start forever**,
# with the message pointing at an instance that does not exist and even attaching
# "existing pids" — and that pid list is scanned by a different diagnostic
# function, unrelated to the lock.
#
# Measured locally (Windows 11 / CPython, 2026-09-08):
#
#   msvcrt.locking(fd, LK_NBLCK, 1) on an already-locked region → errno=13  EACCES
#   msvcrt.locking(fd, LK_LOCK,  1) retry failure                → errno=36  EDEADLOCK
#   msvcrt.locking(a broken fd)                                   → errno=9   EBADF
#   msvcrt.locking(fd, LK_NBLCK, -1)                              → errno=22  EINVAL
#
# The first two are "someone holds it", the last two are "cannot lock" —
# **distinguishable**, so there is no reason to conflate them.

_CONTENDED_ERRNOS = [
    ("EACCES", 13),        # Windows msvcrt: measured value
    ("EAGAIN", None),      # POSIX flock
    ("EWOULDBLOCK", None),  # POSIX flock (a different value from EAGAIN on Windows, see below)
    ("EDEADLK", None),     # = EDEADLOCK(36), the blocking msvcrt's answer
]


@pytest.mark.parametrize(("name", "expected_value"), _CONTENDED_ERRNOS)
def test_a_contended_lock_errno_still_means_another_instance(
        name, expected_value, tmp_path):
    """The errnos that mean "held by someone else" must still return `None` and
    close the fd.

    This is the direction where the whitelist is **too tight**: miss any one of
    them and a real second instance gets let through, which is the only reason
    this lock exists.
    """
    import errno as errno_mod
    number = getattr(errno_mod, name, None)
    if number is None:
        pytest.skip(f"this platform has no errno.{name}")
    if expected_value is not None:
        assert number == expected_value, (
            f"errno.{name} is {number} on this machine, not the measured-recorded "
            f"{expected_value} — the whitelist's comment needs updating")

    fake_fcntl = _FakeFcntl(error=OSError(number, os.strerror(number)))
    with _posix_branch(fake_fcntl) as fake_os:
        lock = acquire_single_instance_lock(_lock_path(tmp_path))
        assert lock is None, (
            f"errno={number} ({name}) means another instance is running, must "
            "return None. Returning a degraded shell lets a second supervisor in.")
        assert fake_os.closed == fake_os.opened, "the stand-aside path must close the fd"


_UNDECIDABLE_ERRNOS = ["EBADF", "EINVAL", "ENOLCK", "ENOSYS", "EPERM", "EIO"]


@pytest.mark.parametrize("name", _UNDECIDABLE_ERRNOS)
def test_an_unknown_lock_errno_degrades_instead_of_claiming_another_instance(
        name, tmp_path):
    """An errno not on the whitelist = "cannot decide" ⇒ a degraded shell, **not**
    `None`.

    **This is the entire value this change bought.** Returning `None` reports a
    concurrency-unrelated filesystem problem as "another instance is already
    running", the launcher refuses to start forever, and the message the user sees
    points at an instance that does not exist, with no clue toward the real cause.
    """
    import errno as errno_mod
    number = getattr(errno_mod, name, None)
    if number is None:
        pytest.skip(f"this platform has no errno.{name}")

    fake_fcntl = _FakeFcntl(error=OSError(number, os.strerror(number)))
    with _posix_branch(fake_fcntl) as fake_os:
        lock = acquire_single_instance_lock(_lock_path(tmp_path))
        assert lock is not None, (
            f"errno={number} ({name}) means the locking mechanism itself has a "
            "problem, not that another instance exists. Returning None makes the "
            "launcher refuse to start forever, with the error pointing at a "
            "nonexistent instance.")
        assert lock.degraded is True, "when allowing, it must honestly mark itself degraded"
        # This path deliberately keeps the fd (consistent with the `ImportError`
        # path); `release()` closes it.
        assert fake_os.closed == []
        lock.release()


def test_an_oserror_without_an_errno_is_also_undecidable(tmp_path):
    """An `OSError`'s `errno` can be `None`. That is all the more "cannot decide",
    not "someone holds it".

    `None in frozenset_of_ints` is False, so this **naturally** lands on the
    degrade side; a test pins it because "casually change the default to return
    None" looks harmless.
    """
    fake_fcntl = _FakeFcntl(error=OSError("the lock call broke, no errno"))
    with _posix_branch(fake_fcntl):
        lock = acquire_single_instance_lock(_lock_path(tmp_path))
        assert lock is not None and lock.degraded is True
        lock.release()


def test_the_contended_errno_list_covers_both_platforms():
    """The whitelist must cover both the Windows and POSIX answers, and must not
    assume the two are equal.

    `EWOULDBLOCK` on Linux is just `EAGAIN` (both 11), **but on Windows CPython it
    is not**: measured `EAGAIN == 11`, `EWOULDBLOCK == 10035` (Winsock's
    WSAEWOULDBLOCK). Writing only one of them misses the "already held" answer on
    one platform.
    """
    import errno as errno_mod
    from _supervisor import _LOCK_HELD_ERRNOS  # noqa: SLF001

    for name in ("EACCES", "EAGAIN", "EWOULDBLOCK", "EDEADLK"):
        number = getattr(errno_mod, name, None)
        if number is not None:
            assert number in _LOCK_HELD_ERRNOS, (
                f"errno.{name} ({number}) is not in the whitelist — that platform's "
                "'already held' would be misread as 'the lock broke', and duplicate "
                "instances get let through.")
    for name in ("EBADF", "EINVAL", "ENOLCK"):
        number = getattr(errno_mod, name, None)
        if number is not None:
            assert number not in _LOCK_HELD_ERRNOS, (
                f"errno.{name} ({number}) should not be in the whitelist — that is "
                "'cannot lock', and treating it as 'someone holds it' makes the "
                "launcher refuse to start forever.")


# ---------------------------------------------------------------------------
# `degraded` must be announced (2026-09-08)
# ---------------------------------------------------------------------------
#
# Before this, `degraded` was **read nowhere** in production code — only the three
# places in `_supervisor.py` set it, and everything else reading it was tests.
# That means "mutual-exclusion protection is not in effect on this machine" is
# something the user would never know: the launcher starts as usual, the log looks
# entirely normal, and duplicate instances surface days later with an entirely
# different symptom (on the webrunner side, two batches nuclear-sweeping each
# other).
#
# A silent degradation is the same thing as not having this lock. The errno
# whitelist above changed "cannot decide" from "refuse to start" to "start
# anyway"; **this section is the other half of that change**: allowing is fine,
# but silently allowing is not.


def _launcher_says_with_a_degraded_lock(launcher, tmp_path, monkeypatch):
    """Give the launcher a degraded lock and collect the `(message, err)` it
    `say()`s.

    `acquire_single_instance_lock` is swapped directly for a stub that returns a
    shell — **without actually manufacturing a filesystem where locking fails**.
    The spawn entry points are all swapped for an interceptor that raises (it must
    not return an rc: the bot's `while True:` treats a clean exit as a cue to
    respawn, and the test would hang rather than go red).
    """
    module = _load_launcher(launcher)
    work = tmp_path / launcher
    work.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(module, "LOCK_FILE", work / "test.lock")
    monkeypatch.setattr(module, "WEBRUNNER_LOG" if "webrunner" in launcher
                        else "BOT_LOG", work / "test.log")
    monkeypatch.setattr(sys, "argv", [launcher])

    from _supervisor import InstanceLock
    shell = InstanceLock(None, str(work / "test.lock"), degraded=True)
    monkeypatch.setattr(module, "acquire_single_instance_lock",
                        lambda _path: shell)

    said: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        module, "say",
        lambda _log, message, **kwargs: said.append(
            (message, bool(kwargs.get("err", False)))))

    class _Spawned(Exception):
        pass

    for hook in ("_supervise", "stream_child"):
        if hasattr(module, hook):
            def _boom(*_a, **_k):
                raise _Spawned
            monkeypatch.setattr(module, hook, _boom)

    try:
        module.main()
    except _Spawned:
        pass                    # reaching spawn is enough; this does not care about what follows
    return said


def _degraded_lines(said):
    # The substring below matches the launchers' Chinese degraded-lock line, which
    # lives in start_discord_bot.py / start_webrunner.py (out of scope here), so it
    # is intentionally left untranslated.
    return [(message, err) for message, err in said
            if "單一實例保護" in message]


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_every_launcher_announces_a_degraded_lock(launcher, tmp_path,
                                                  monkeypatch):
    """When mutual-exclusion protection is not in effect, both launchers must
    speak up.

    This is the companion to the errno-whitelist change: allowing an unknown lock
    error is a deliberate trade-off, but **allowing must not be silent**. Without
    this line, "the lock on this machine does nothing" becomes something you learn
    only by reading the source.
    """
    said = _launcher_says_with_a_degraded_lock(launcher, tmp_path, monkeypatch)
    lines = _degraded_lines(said)
    assert lines, (
        f"{launcher} got a degraded lock but said nothing. The user thinks "
        "duplicate startup is blocked when it is not.")

    for message, err in lines:
        # A degradation is **not** an error; do not send it to stderr where it
        # looks like a failure.
        assert err is False, (
            f"{launcher} sent the degradation message as an error (err=True). It "
            "is a degradation, not a failure; mixed in with errors it gets skipped "
            "as noise.")
        # The message must not carry a host absolute path (the lock file path is
        # the most tempting thing to stuff in).
        assert ":\\" not in message and ":/" not in message, (
            f"{launcher}'s degradation message carries a host absolute path: {message!r}")
        assert str(tmp_path) not in message


def test_both_launchers_use_the_same_degraded_wording(tmp_path, monkeypatch):
    """The two must be worded identically — this is a classic spot where "two
    implementations" drift apart."""
    wordings = {}
    for launcher in _LAUNCHERS:
        said = _launcher_says_with_a_degraded_lock(launcher, tmp_path,
                                                   monkeypatch)
        lines = _degraded_lines(said)
        assert lines, f"{launcher} did not announce the degradation"
        wordings[launcher] = [message for message, _err in lines]

    first, second = (wordings[name] for name in _LAUNCHERS)
    assert first == second, (
        "the two launchers' degradation messages disagree. The same thing said "
        "two ways in two places makes a log reader think they are two different "
        f"situations:\n  {first}\n  {second}")


# ---------------------------------------------------------------------------
# `other_launcher_pids`: a diagnostic **must never** crash the launcher
# ---------------------------------------------------------------------------


def test_other_launcher_pids_without_psutil_returns_empty():
    """With no psutil, return an empty list — this is a diagnostic for the
    message; deciding is the lock's job.

    Letting it re-raise `ImportError` would turn a "mention the existing pids in
    passing" feature into the reason the launcher cannot start.
    """
    from _supervisor import other_launcher_pids
    with _NoModule("psutil"):
        assert other_launcher_pids("start_webrunner.py") == []


def test_other_launcher_pids_skips_a_process_it_cannot_parse():
    """When one process record is broken, skip it, **do not** throw away the whole
    scan result.

    What psutil returns is not ours to control (`cmdline()` can return odd values
    when permissions are insufficient or a process just died). Missing one entry
    only drops one pid from the message; losing the whole thing shows the user "no
    other instances" — the exact opposite of the truth.
    """
    from _supervisor import other_launcher_pids
    procs = [(11, "python.exe", 12345, 1),          # cmdline is not a sequence
             (12, "python.exe",
              ["py.exe", "D:/Work/Example/start_webrunner.py"], 1)]
    assert other_launcher_pids("start_webrunner.py", self_pid=99,
                               procs=procs) == [12]


def test_other_launcher_pids_keeps_what_it_found_when_the_scan_blows_up():
    """When the scanner itself blows up midway, still report what it already
    found.

    This deliberately uses **different** input from the previous test: there the
    exception happens in per-entry parsing (inner), here in the iterator itself
    (outer). Using the same input would let the two protection layers mask each
    other — deleting either layer stays green.
    """
    from _supervisor import other_launcher_pids

    def _procs():
        yield (11, "python.exe",
               ["py.exe", "D:/Work/Example/start_webrunner.py"], 1)
        raise RuntimeError("psutil blew up midway through the scan")

    assert other_launcher_pids("start_webrunner.py", self_pid=99,
                               procs=_procs()) == [11]


# ---------------------------------------------------------------------------
# `trim_log` / `log_write` / `echo_line` / `pump_stream`: a broken log must not
# spread
# ---------------------------------------------------------------------------


def test_trim_log_stays_quiet_when_the_file_cannot_be_rewritten(tmp_path,
                                                                capsys):
    """A failed trim can only be "no trim this time", not "the supervisor dies".

    `trim_log` is the documented atomic-write **exception** in this project (it
    overwrites in place, because this file has two live append handles at once), so
    it has no `os.replace` fallback — a mid-write failure is more likely than
    elsewhere, so this path all the more needs verifying.

    Also pins "a failure must leave a message": swallowing it entirely lets the
    log grow without bound while saying not a word (which is exactly what would
    happen after switching to an atomic write).
    """
    import _supervisor as sup

    path = tmp_path / "readonly.log"
    path.write_text("keep-me\n" * 200, encoding="utf-8")
    before = path.read_bytes()
    os.chmod(path, 0o444)               # on Windows = set the read-only attribute
    try:
        sup.trim_log(path, max_bytes=100, keep_bytes=50)
    finally:
        os.chmod(path, 0o644)
    assert path.read_bytes() == before, "could not write but the file was changed"
    assert "trim_log" in capsys.readouterr().err, (
        "a failed trim must leave a line of diagnostics; failing silently lets the "
        "log grow without bound with nobody knowing.")


def test_log_write_survives_a_dead_log_handle(tmp_path):
    """A broken log handle only drops one line; it must not take the pump thread
    away.

    **A closed stream throws `ValueError`, not `OSError`**, and both must be
    caught — which is also why `log_write`'s `except` is `(OSError, ValueError)`.
    """
    import _supervisor as sup

    handle = (tmp_path / "closed.log").open("a", encoding="utf-8")
    handle.close()
    sup.log_write(handle, "falls into a closed file\n")     # ValueError must not escape

    class _FullDisk:
        def write(self, _text):
            raise OSError(28, "No space left on device")

    sup.log_write(_FullDisk(), "disk is full\n")      # OSError must not escape


def test_echo_line_falls_back_when_the_console_cannot_encode():
    """When the launcher's stdout is redirected to a cp950 file, an unencodable
    character must fall back to a replacement character.

    **Do not use Japanese kana as test data**: measured, cp950 (Big5) **can
    encode** kana (`"かな".encode("cp950")` = `b"cf af cf ce"`), so a kana test
    never takes the fallback path yet still looks green. Use an emoji here — that
    is something Big5 really lacks.

    This is a real situation on this machine, not a theoretical one:
    `locale.getpreferredencoding(False)` is cp950, and the queue content contains
    non-Chinese characters.
    """
    import _supervisor as sup

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp950", errors="strict",
                              write_through=True)
    saved = sys.stdout
    sys.stdout = stream
    try:
        sup.echo_line("進度 \U0001F600 33/75\n")
    finally:
        sys.stdout = saved
    stream.flush()
    text = raw.getvalue().decode("cp950")
    assert "進度" in text and "33/75" in text, (
        f"the fallback write lost the whole line: {text!r}. **Only that one "
        f"character** is unencodable; the rest must still be visible.")
    assert "?" in text, f"the unencodable character was not replaced: {text!r}"


class _EncodeThenFail:
    """First write throws `UnicodeEncodeError`, the fallback write throws `exc`."""

    encoding = "cp950"

    def __init__(self, exc):
        self._exc = exc
        self.writes = 0

    def write(self, text):
        self.writes += 1
        if self.writes == 1:
            raise UnicodeEncodeError("cp950", text, 0, 1,
                                     "illegal multibyte sequence")
        raise self._exc

    def flush(self):
        return None


@pytest.mark.parametrize("exc", [
    OSError(28, "No space left on device"),      # the disk of the redirected file is full
    ValueError("I/O operation on closed file."),  # the pipe's read end is already closed
    LookupError("unknown encoding: bogus"),      # the stream lies about its encoding
])
def test_a_console_that_breaks_during_the_fallback_never_escapes(exc):
    """**Real defect (fixed 2026-09-07)**: the fallback write was under no
    protection at all.

    The fallback write used to live inside the `except UnicodeEncodeError:` block,
    with `except OSError: pass` hung below — but **an exception thrown from inside
    an `except` block is not caught by another `except` of the same `try`**.
    Measured: the first write threw `UnicodeEncodeError`, the fallback threw
    `OSError(28)`, and that `OSError` shot straight out of `echo_line`.

    The consequence is not one dropped log line: `echo_line` runs on the pump
    thread, the exception terminates the whole pump loop, and the child then wedges
    in its own filled pipe — the supervisor is still alive, the batch is stuck, and
    it says nothing. This test pins "one log line must never kill the supervisor".
    """
    import _supervisor as sup

    stream = _EncodeThenFail(exc)
    saved = sys.stdout
    sys.stdout = stream
    try:
        sup.echo_line("進度 \U0001F600\n")   # must not throw
    finally:
        sys.stdout = saved
    assert stream.writes == 2, (
        f"the fallback write was never even attempted (writes={stream.writes})")


def test_a_broken_console_does_not_stop_the_pump():
    """When the console write breaks, the pump **must keep going** — this is the
    regression test for that deadlock.

    `pump_stream`'s `except (OSError, ValueError)` wraps the whole loop, so as long
    as `echo_line` can throw an exception, one broken console equals the entire
    pump stopping. Measured (before the fix): 3 lines, only 1 pumped. The child
    then fills the ~64 KB pipe buffer and **hangs forever on the next print** —
    much worse than "no log", because the batch stalls entirely rather than
    dropping a log.
    """
    import _supervisor as sup

    class _AlwaysClosed:
        encoding = "cp950"

        def write(self, _text):
            raise ValueError("I/O operation on closed file.")

        def flush(self):
            return None

    lines = ["第一行\n", "第二行\n", "第三行\n"]
    log = io.StringIO()
    saved = sys.stdout
    sys.stdout = _AlwaysClosed()
    try:
        sup.pump_stream(iter(lines), log)
    finally:
        sys.stdout = saved
    assert log.getvalue().count("行") == 3, (
        f"the broken console took the pump down with it; only pumped {log.getvalue()!r}. "
        "The child would then wedge in its own filled pipe.")


def test_pump_stream_tolerates_no_stream_at_all():
    """When `proc.stdout` is `None` (the caller gave no PIPE), finish quietly."""
    import _supervisor as sup
    sup.pump_stream(None, None)


def test_pump_stream_stops_quietly_when_the_pipe_dies():
    """When the pipe breaks midway: keep what was already read, and do not
    re-raise.

    The pump runs on a daemon thread, and an exception it throws only prints a
    context-less `Exception in thread`, after which `stream_child` keeps waiting at
    `proc.wait()` — it looks like a hang, but really the pump has died.
    """
    import _supervisor as sup

    class _DyingPipe:
        def __iter__(self):
            return self

        def __next__(self):
            if not hasattr(self, "_done"):
                self._done = True
                return "keep this line from before it broke\n"
            raise OSError(22, "The handle is invalid")

    log = io.StringIO()
    sup.pump_stream(_DyingPipe(), log)          # must not throw
    assert "keep this line from before it broke" in log.getvalue()


# ---------------------------------------------------------------------------
# `stream_child`: every error path after the child is already up
# ---------------------------------------------------------------------------


class _ProcWrapper:
    """Wrap a real `Popen`, swapping only the one behaviour under test and
    delegating the rest verbatim."""

    def __init__(self, proc):
        self._proc = proc

    def __getattr__(self, name):
        if name == "_proc":                     # avoid infinite recursion before `_proc` is set
            raise AttributeError(name)
        return getattr(self._proc, name)


def _popen_shim(monkeypatch, wrap):
    """Swap the `subprocess` in the `_supervisor` namespace for a thin shell: still
    start the real child, just let `wrap` tamper before returning.

    Swap only the `_supervisor.subprocess` **name**, not the stdlib module itself —
    the latter is process-wide, so another thread spawning at that moment would be
    caught along with it.
    """
    import _supervisor as sup

    real = subprocess

    class _Shim:
        PIPE = real.PIPE
        STDOUT = real.STDOUT
        TimeoutExpired = real.TimeoutExpired

        @staticmethod
        def Popen(*args, **kwargs):             # noqa: N802  # match stdlib naming
            return wrap(real.Popen(*args, **kwargs))

    monkeypatch.setattr(sup, "subprocess", _Shim)


_SLEEPY_CHILD = (
    "import sys, time\n"
    "print('CHILD-UP')\n"
    "sys.stdout.flush()\n"
    "time.sleep(30)\n"
)


def test_ctrl_c_reaps_the_child_instead_of_orphaning_it(tmp_path, monkeypatch):
    """Ctrl+C: the exception must be re-raised (the launcher uses it to wrap up),
    but the child must be reaped clean first.

    Just re-raising `KeyboardInterrupt` and walking away leaves an orphan — on the
    webrunner side a whole Chrome tree along with it, and the next start becomes
    two stacks fighting over the same `.chrome_profile/`. Here we use a real child
    + a real pipe, only swapping `proc.wait()` for one that throws
    `KeyboardInterrupt` (a test cannot send a real Ctrl+C to its own console group
    without taking pytest down with it).
    """
    import _supervisor as sup

    log_path = tmp_path / "ki.log"

    class _InterruptOnWait(_ProcWrapper):
        def __init__(self, proc):
            super().__init__(proc)
            self.interrupted = False

        def wait(self, timeout=None):
            # Intercept only the `stream_child` call (no timeout); the `reap_child`
            # calls that carry a timeout must run for real, or what gets verified is
            # not the reaping flow itself.
            if timeout is None and not self.interrupted:
                # Wait for the child to actually speak, then send Ctrl+C. It used to
                # send immediately, silently assuming "the child prints its first
                # line within the 0.5s grace period" — on 2026-09-22 an IDE-launched
                # environment carried a `sitecustomize` that made every Python child
                # take 1.2s to start, and this test went red on both interpreters at
                # once, with the code untouched. What to verify is "what was said
                # before Ctrl+C is not lost", which presupposes it was said; the wait
                # is bounded, and on timeout it sends anyway so the assertion below
                # states the reason.
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    try:
                        if "CHILD-UP" in log_path.read_text(
                                encoding="utf-8", errors="replace"):
                            break
                    except OSError:
                        pass
                    time.sleep(0.05)
                self.interrupted = True
                raise KeyboardInterrupt
            return self._proc.wait(timeout=timeout)

    holder = {}

    def _wrap(proc):
        holder["proc"] = _InterruptOnWait(proc)
        return holder["proc"]

    _popen_shim(monkeypatch, _wrap)

    script = tmp_path / "sleepy.py"
    script.write_text(_SLEEPY_CHILD, encoding="utf-8")
    try:
        with log_path.open("a", encoding="utf-8", buffering=1) as handle:
            with pytest.raises(KeyboardInterrupt):
                sup.stream_child([sys.executable, "-u", str(script)], handle,
                                 cwd=str(tmp_path), pump_name="ki-pump",
                                 grace_sec=0.5, kill_sec=10.0)
    finally:
        proc = holder.get("proc")
        if proc is not None and proc.poll() is None:   # safety net: never leave an orphan
            proc.kill()
            proc.wait(timeout=30)

    proc = holder["proc"]
    assert proc.poll() is not None, (
        "the child is still alive after Ctrl+C — this is exactly the source of "
        "those orphans holding a whole Chrome tree.")
    log = log_path.read_text(encoding="utf-8", errors="replace")
    assert "CHILD-UP" in log, (
        "what the child said before Ctrl+C is gone; that output is exactly what "
        "you investigate afterward.")
    assert "terminating" in log, (
        "the child did not finish within the grace period yet terminate was never "
        "reached; reaping is courtesy first, force after.")
    assert not [t for t in threading.enumerate() if t.name == "ki-pump"], (
        "the pump thread was not reaped.")


def test_a_stdout_that_refuses_to_close_does_not_swallow_the_exit_code(
        tmp_path, monkeypatch):
    """When `proc.stdout.close()` fails on wrap-up, it must not turn the rc already
    obtained into an exception.

    This line lives in `finally`, so an exception it throws **replaces**
    `return rc` — the supervisor never gets the child's exit code, both rapid-fail
    giveup and `child_exit_is_fatal` stop working at once, and the real reason (why
    the child exited) has already been masked.
    """
    import _supervisor as sup

    class _CloseFails:
        def __init__(self, stream):
            self._stream = stream
            self.attempts = 0

        def __iter__(self):
            return iter(self._stream)

        def close(self):
            self.attempts += 1
            self._stream.close()        # really close it, or the fd leaks
            raise OSError(5, "Input/output error")

    class _StdoutCloseFails(_ProcWrapper):
        def __init__(self, proc):
            super().__init__(proc)
            self._stdout = _CloseFails(proc.stdout)

        @property
        def stdout(self):
            return self._stdout

    holder = {}

    def _wrap(proc):
        holder["proc"] = _StdoutCloseFails(proc)
        return holder["proc"]

    _popen_shim(monkeypatch, _wrap)

    script = tmp_path / "quick.py"
    script.write_text("import sys\nprint('bye')\nsys.exit(9)\n",
                      encoding="utf-8")
    console = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(console):
        rc = sup.stream_child([sys.executable, "-u", str(script)], None,
                              cwd=str(tmp_path), pump_name="close-pump")
    assert rc == 9, f"the wrap-up close failure swallowed the rc (rc={rc})"
    assert holder["proc"].stdout.attempts == 1


def test_the_spawn_hook_runs_before_the_child_is_waited_on(tmp_path):
    """`on_spawn` must run **before** `proc.wait()`, and get the real pid.

    `start_webrunner._on_spawn` writes `webrunner.pid` and releases the Chrome slot
    there; a step late opens a "slot empty, pid not yet written" window, and if the
    verifier takes the slot in that instant it judges nobody is running and opens a
    second Chrome stack.
    """
    seen = []
    rc, log, _console = _run_child(tmp_path, "print('hi')\n",
                                   on_spawn=lambda proc: seen.append(proc.pid))
    assert rc == 0
    assert seen and isinstance(seen[0], int), "on_spawn was not called"
    assert "hi" in log


def test_a_failing_spawn_hook_does_not_leave_an_orphan(tmp_path):
    """**Real defect (fixed 2026-09-07)**: a spawn-hook failure leaves an unreaped
    child.

    `on_spawn` does real I/O — `start_webrunner._on_spawn` writes `webrunner.pid` —
    and a full disk or wrong permissions throw `OSError`. Before the fix that
    exception was re-raised straight away, while the child **is already up**: its
    stdout is a PIPE that nobody pumps and nobody `wait`s on. Its next print wedges
    in the filled pipe, and it is holding a whole Chrome tree. The supervisor itself
    dying while the batch sits there stuck is the hardest kind of ending to
    diagnose.

    The exception must still be re-raised (failing to write the pid file is serious,
    and swallowing it would let the verifier barge into a running batch), but
    **reap before re-raising**.
    """
    import contextlib
    import _supervisor as sup

    script = tmp_path / "sleepy.py"
    script.write_text(_SLEEPY_CHILD, encoding="utf-8")
    holder = {}

    def _boom(proc):
        holder["proc"] = proc
        raise OSError(28, "No space left on device")

    console = io.StringIO()
    try:
        with contextlib.redirect_stdout(console):
            with pytest.raises(OSError):
                sup.stream_child([sys.executable, "-u", str(script)], None,
                                 cwd=str(tmp_path), on_spawn=_boom,
                                 pump_name="boom-pump", kill_sec=10.0)
    finally:
        proc = holder.get("proc")
        if proc is not None and proc.poll() is None:   # safety net: never leave an orphan
            proc.kill()
            proc.wait(timeout=30)
            pytest.fail(
                "the child is still alive after the spawn hook failed. Its stdout "
                "is a PIPE nobody pumps, its next print wedges, and it is holding a "
                "whole Chrome tree.")

    assert holder["proc"].returncode is not None, (
        "the child was not reaped — `stream_child` started it, so it is responsible "
        "for reaping it.")


def test_a_child_that_survives_reaping_does_not_wedge_the_shutdown(
        tmp_path, monkeypatch):
    """When reaping fails and the child is still alive, the wrap-up line must not
    wedge the supervisor permanently.

    Measured (2026-09-07, local Windows 11 / CPython 3.14): calling
    `proc.stdout.close()` while the pump thread is stuck in `read()` throws **no**
    exception and does not wrest the stream away from the pump — it contends for the
    same lock and blocks until that read returns, measured **19.05 seconds**,
    exactly how long the child stayed alive.

    The normal path never reaches this (`proc.wait()` returning means the child is
    dead, pipe EOF, the pump ends immediately); what reaches it is the "reap_child
    itself failed after Ctrl+C" path. Its outcome is the hardest to diagnose: the
    launcher stops forever at the last line of wrapping up, **while still holding
    the single-instance lock**, so nobody can restart and no message shows on the
    console — the thing that would print the message is the thing that is stuck.
    """
    import contextlib
    import _supervisor as sup

    script = tmp_path / "sleepy.py"
    script.write_text(_SLEEPY_CHILD, encoding="utf-8")
    real_procs = []

    class _SurvivesReaping(_ProcWrapper):
        """A child that cannot be reaped no matter what after Ctrl+C (both
        terminate and kill fail)."""

        def __init__(self, proc):
            super().__init__(proc)
            self.interrupted = False

        def wait(self, timeout=None):
            if timeout is None and not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            raise subprocess.TimeoutExpired(cmd="child", timeout=timeout)

        def terminate(self):
            raise OSError(5, "Access is denied")

        def kill(self):
            raise OSError(5, "Access is denied")

    def _wrap(proc):
        real_procs.append(proc)
        return _SurvivesReaping(proc)

    _popen_shim(monkeypatch, _wrap)
    # Shorten the join wait so the "did it wedge" gap is 0.5 seconds vs the child's
    # whole lifetime, rather than two close numbers — a timing assertion only avoids
    # being a randomly-red guard when the gap is large enough.
    monkeypatch.setattr(sup, "_LOG_PUMP_JOIN_SEC", 0.5)

    console = io.StringIO()
    started = time.monotonic()
    try:
        with contextlib.redirect_stdout(console):
            with pytest.raises(OSError):
                sup.stream_child([sys.executable, "-u", str(script)], None,
                                 cwd=str(tmp_path), pump_name="wedge-pump",
                                 grace_sec=0.2, kill_sec=0.2)
        elapsed = time.monotonic() - started
    finally:
        for proc in real_procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)
    assert elapsed < 8.0, (
        f"wrap-up wedged for {elapsed:.1f} seconds (the child lives 30). Closing "
        "the stream while the pump is still reading blocks until that read returns "
        "— the supervisor stops here forever, still holding the single-instance "
        "lock, and nobody can restart.")


def test_a_failing_reap_does_not_mask_why_the_spawn_hook_failed(tmp_path,
                                                                monkeypatch):
    """When reaping itself also fails, what is re-raised must still be the
    **original** exception.

    This is the classic error-path failure: cleanup code throws its own exception
    and masks the real cause. Here it would mask "the pid file cannot be written"
    (the thing to handle) with "terminate failed" (an unresolvable surface
    symptom), and the log reader never sees that the former existed.
    """
    import contextlib
    import _supervisor as sup

    script = tmp_path / "sleepy.py"
    script.write_text(_SLEEPY_CHILD, encoding="utf-8")
    real_procs = []

    class _Unreapable(_ProcWrapper):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="child", timeout=timeout)

        def terminate(self):
            raise OSError(5, "Access is denied")

        def kill(self):
            raise OSError(5, "Access is denied")

    def _wrap(proc):
        real_procs.append(proc)
        return _Unreapable(proc)

    _popen_shim(monkeypatch, _wrap)

    def _boom(_proc):
        raise OSError(28, "No space left on device")

    console = io.StringIO()
    try:
        with contextlib.redirect_stdout(console):
            with pytest.raises(OSError) as info:
                sup.stream_child([sys.executable, "-u", str(script)], None,
                                 cwd=str(tmp_path), on_spawn=_boom,
                                 pump_name="mask-pump", kill_sec=1.0)
        assert info.value.errno == 28, (
            f"what was re-raised is the reap-failure exception (errno={info.value.errno}); "
            "the original 'pid file cannot be written' got masked.")
    finally:
        for proc in real_procs:                 # this test deliberately fails reaping, so reap them here
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)


# ---------------------------------------------------------------------------
# When `webrunner.pid` cannot be read, the launcher must fall to the conservative
# side (2026-09-07)
#
# `start_webrunner._live_webrunner_pid` used to return `None` for both "the file
# does not exist" and "the file is there but cannot be read", and `None` at the
# caller means **"no batch is running, you can start one"** — falling to the
# optimistic side while unable to decide. The cost is a second batch starting on
# the same machine: two webrunners fighting over the same `.chrome_profile/`, each
# nuclear-sweeping the other's Chrome, picking the same `todo_*.md` items twice,
# while both records look normal. CLAUDE.md's Windows PID-liveness hard rule is
# explicit about this class of decision: where you ask "should I refuse to start /
# stand aside?", when you cannot decide, fall to the conservative side. The sister
# function `verify_browser._live_webrunner_pid` was fixed the same day to
# `(pid, decided)`, and this follows the same shape.
#
# The second hole fixed along the way: `UnicodeDecodeError` is a subclass of
# `ValueError`, **not** `OSError`, so `except (FileNotFoundError, OSError)` does
# not catch it — when the pid file content is not valid UTF-8, both file-reading
# functions blow straight through. `_clear_pid_if_ours`'s docstring even says
# "never raises", and it is called inside `finally`.
#
# This group of tests **never touches the production `webrunner.pid`** (this
# machine often has a batch that has run for tens of hours using it), always
# monkeypatching to `tmp_path`; `_chrome_slot` is always swapped for a stub too, to
# avoid touching the real slot lock in the repo root.
# ---------------------------------------------------------------------------

WEBRUNNER_LAUNCHER = "start_webrunner.py"


class _FakeSlot:
    """A stub close enough to `_chrome_slot`: always gets the slot, records
    releases, controllable pid liveness.

    Swapping it is a **safety requirement**, not a convenience: the real
    `_chrome_slot` touches the repo root's `chrome_slot.lock`, and the production
    batch is using that slot.
    """

    def __init__(self, *, pid_alive=True):
        self.released = []
        self._alive = pid_alive

    def acquire(self, owner, *, timeout=0.0, label=""):
        return True

    def release(self, owner):
        self.released.append(owner)

    def _pid_alive(self, pid):
        return self._alive


@pytest.fixture(autouse=True)
def _no_real_network_probe(monkeypatch):
    """`_supervise` in this file must not touch the real network.

    When the child exits non-zero, the launcher first asks "can the host reach the
    network", and if it judges the network is down it excludes that from the
    give-up threshold and instead waits **indefinitely** for the network to come
    back. There used to be no stub here, so every test that fails the child really
    connected outward: a network hiccup, or a host busy enough for the probe to time
    out, would keep a fast failure from counting (one really went red under full
    load on 2026-09-23), and a real outage would wedge the whole suite there. The
    outage path has its own coverage in `test_batch_recovery`. What is swapped is
    the actual module object the launcher holds (`axiomatic._connectivity`).
    """
    from axiomatic import _connectivity as launcher_connectivity  # noqa: PLC0415

    def _no_wait(*_args, **_kwargs):
        raise AssertionError("the launcher thought the network was down and began waiting — this file's tests should not reach here")

    monkeypatch.setattr(launcher_connectivity, "is_online", lambda *_a, **_k: True)
    monkeypatch.setattr(launcher_connectivity, "wait_until_online", _no_wait)


def test_the_launcher_in_this_file_never_probes_the_real_network(monkeypatch, tmp_path):
    """The stub above really hooks the module the launcher uses: even when every
    low-level connection fails, the launcher still returns "network is up"."""
    import socket  # noqa: PLC0415

    def _refuse(*_args, **_kwargs):
        raise OSError("no network in tests")

    monkeypatch.setattr(socket, "create_connection", _refuse)
    module, _slot = _launcher_with_pid_file(monkeypatch, tmp_path, None)
    assert module._network_is_up() is True


def _launcher_with_pid_file(monkeypatch, tmp_path, content, *, pid_alive=True):
    """Load the launcher and point `WEBRUNNER_PID_FILE` at `tmp_path`.

    `content` is bytes (deliberately not str — so the "not valid UTF-8" path is
    reachable), `None` = create no file. Returns `(module, fake_slot)`.
    """
    module = _load_launcher(WEBRUNNER_LAUNCHER)
    path = tmp_path / "webrunner.pid"
    if content is not None:
        path.write_bytes(content)
    monkeypatch.setattr(module, "WEBRUNNER_PID_FILE", path)
    slot = _FakeSlot(pid_alive=pid_alive)
    monkeypatch.setattr(module, "_chrome_slot", slot)
    return module, slot


def test_a_missing_pid_file_means_there_is_really_no_batch(monkeypatch,
                                                           tmp_path):
    """A missing file is a **decidable** answer: there really is no batch, you can
    start one."""
    module, _ = _launcher_with_pid_file(monkeypatch, tmp_path, None)
    assert module._live_webrunner_pid() == (None, True)


def test_a_live_pid_is_reported_as_a_running_batch(monkeypatch, tmp_path):
    module, _ = _launcher_with_pid_file(monkeypatch, tmp_path, b"4242")
    assert module._live_webrunner_pid() == (4242, True)


def test_a_dead_pid_reads_as_no_batch(monkeypatch, tmp_path):
    """A dead pid is a **decidable** "no batch" — a hard kill leaves this kind of
    file, and this must not be conflated with "cannot be read", or one hard kill
    leaves the launcher unable to start again."""
    module, _ = _launcher_with_pid_file(monkeypatch, tmp_path, b"999999",
                                        pid_alive=False)
    assert module._live_webrunner_pid() == (None, True)


def test_an_undecodable_pid_file_is_undecidable_not_empty(monkeypatch,
                                                          tmp_path):
    """When the content is not valid UTF-8, it must not be treated as "no batch is
    running".

    Two things pinned together: this function **must not raise**
    (`UnicodeDecodeError` is not `OSError`), and what it returns must be "cannot
    decide" rather than "no batch" — the latter would make the launcher start
    another webrunner next to the production batch.
    """
    module, _ = _launcher_with_pid_file(monkeypatch, tmp_path,
                                        b"\xff\xfe\x00\x80")
    pid, decided = module._live_webrunner_pid()      # must not raise
    assert pid is None
    assert decided is False, (
        "the content could not be read but it reported 'decidable, no batch' — the "
        "launcher would start another webrunner on that basis")


def test_a_non_numeric_pid_file_is_undecidable(monkeypatch, tmp_path):
    """Readable but not a number (including an empty string) is likewise "cannot
    decide", not "no batch"."""
    module, _ = _launcher_with_pid_file(monkeypatch, tmp_path,
                                        "not a number".encode("utf-8"))
    assert module._live_webrunner_pid() == (None, False)


def test_an_unreadable_pid_file_is_undecidable(monkeypatch, tmp_path):
    """A read that itself throws `OSError` (permissions, a locked file) must also
    fall to the conservative side."""
    module, _ = _launcher_with_pid_file(monkeypatch, tmp_path, b"123")
    real = tmp_path / "webrunner.pid"

    class _Locked:
        def exists(self):
            return True

        def read_text(self, *_a, **_k):
            raise PermissionError("locked")

    monkeypatch.setattr(module, "WEBRUNNER_PID_FILE", _Locked())
    assert module._live_webrunner_pid() == (None, False)
    assert real.exists()                              # the real file was not touched


def _run_one_round(monkeypatch, tmp_path, content, *, pid_alive=True, rc=0):
    """Run one round of `_supervise` under the stub slot, return
    `(module, exit_code, whether it spawned, slot)`.

    `stream_child` is swapped for a spy, so **this test never actually starts a
    webrunner** — a webrunner on startup unconditionally nuclear-sweeps all Chrome,
    and testing a safety mechanism should not risk the very thing it guards against.
    """
    module, slot = _launcher_with_pid_file(monkeypatch, tmp_path, content,
                                           pid_alive=pid_alive)
    spawned = []

    def _spy(cmd, _log, **_kwargs):
        spawned.append(cmd)
        return rc

    monkeypatch.setattr(module, "stream_child", _spy)
    code = module._supervise(
        ["python", "-u", "webrunner.py"], "selenium", None,
        backoff_min=5.0, backoff_max=300.0, healthy_sec=60.0,
        rapid_threshold_sec=30.0, rapid_giveup=3, zero_progress_giveup=3)
    return module, code, bool(spawned), slot


def test_an_undecidable_pid_file_never_spawns_a_webrunner(monkeypatch,
                                                          tmp_path):
    """**The property this change actually guarantees**: when it cannot decide, do
    not start.

    Deliberately a behavioural test rather than an AST scan. The sister function's
    mutation testing proved on the spot that an AST guard is too weak: if it only
    checks "the second return value is consumed, and that name appears", then
    changing `if not decided:` to `if False and not decided:` **still shows the
    name**, the guard stays green, and the behaviour has already reverted to "start
    when it cannot decide". **A property a constant-false condition can bypass can
    only be pinned behaviourally.**
    """
    module, code, spawned, slot = _run_one_round(monkeypatch, tmp_path,
                                                 b"\xff\xfe\x00\x80")
    assert not spawned, "the pid file could not be read but a webrunner was still started"
    assert code == 1, f"standing aside should return rc=1, got {code}"
    assert slot.released == [module.SLOT_OWNER], (
        f"the Chrome slot was not released (or released more than once): {slot.released} — "
        "if the early-exit path fails to release, the slot stays taken until the "
        "staleness timeout reclaims it.")


def test_a_live_batch_never_spawns_a_second_webrunner(monkeypatch, tmp_path):
    """The existing stand-aside path is also pinned behaviourally (it had no test
    at all)."""
    module, code, spawned, slot = _run_one_round(monkeypatch, tmp_path,
                                                 b"4242")
    assert not spawned, "a batch is already running but a second webrunner was still started"
    assert code == 1
    assert slot.released == [module.SLOT_OWNER]


def test_a_clean_machine_really_does_spawn(monkeypatch, tmp_path):
    """The reverse direction, whose absence would be bad: if you pin only "do not
    start when it cannot decide", changing the function to **always** return "do
    not start" also stays green — and then the launcher can never start, worse than
    the original defect, with the symptom "nothing happens when pressed" that
    nobody would look here for."""
    module, code, spawned, slot = _run_one_round(monkeypatch, tmp_path, None,
                                                 rc=0)
    assert spawned, "no batch is running but no webrunner was started"
    assert code == 0
    assert slot.released == [module.SLOT_OWNER]


def test_a_dead_pid_file_still_lets_the_launcher_start(monkeypatch, tmp_path):
    """A pid file left after a hard kill must not block the launcher dead — that is
    a normal recovery situation."""
    _module, code, spawned, _slot = _run_one_round(monkeypatch, tmp_path,
                                                   b"999999", pid_alive=False)
    assert spawned, "a leftover dead pid file blocked the launcher (unable to start after a hard kill)"
    assert code == 0


def test_clearing_the_pid_file_only_touches_our_own_pid(monkeypatch, tmp_path):
    """The bot writes the same file too; deleting someone else's liveness signal =
    the verifier barges into a running batch."""
    module, _ = _launcher_with_pid_file(monkeypatch, tmp_path, b"4242")
    path = tmp_path / "webrunner.pid"
    module._clear_pid_if_ours(999)
    assert path.exists(), "deleted a pid someone else wrote"
    module._clear_pid_if_ours(4242)
    assert not path.exists(), "our own pid was not reclaimed"


def test_clearing_the_pid_file_never_raises_on_an_undecodable_file(monkeypatch,
                                                                   tmp_path):
    """`_clear_pid_if_ours`'s docstring says "never raises", and that must be
    true.

    It is called inside `finally`: an exception thrown from here would mask the
    child's real exit reason, and the record reader never sees that the former
    existed. The direction was already right (do not delete if you cannot read it);
    all that was missing was catching `UnicodeDecodeError`.
    """
    module, _ = _launcher_with_pid_file(monkeypatch, tmp_path,
                                        b"\xff\xfe\x00\x80")
    module._clear_pid_if_ours(4242)                   # must not raise
    assert (tmp_path / "webrunner.pid").exists(), "the content could not be read but the file was deleted"


# ---------------------------------------------------------------------------
# Both launchers must measure "how long the child lived" with a monotonic clock
# (2026-09-07)
#
# `start_webrunner`'s `alive_for` and `start_discord_bot`'s `ran_for` used to both
# be `time.time() - start`. Both are pure **in-process intervals** — the value is
# not written to a file, nor compared against any file mtime — so the criterion
# (see `_chrome_slot.acquire`'s docstring) is clear: use `time.monotonic()`.
#
# The wall clock **jumps** under NTP step corrections, manual clock changes, and VM
# snapshot restores (a timezone or daylight-saving change does not; `time.time()`
# returns UTC epoch seconds). Both directions break:
#
# * Backward → the interval shrinks or even goes negative → a perfectly healthy
#   child is judged a rapid fail → the webrunner one **gives up early**, the bot
#   one grows its backoff all the way up.
# * Forward → it looks like it lived a long time → the rapid-fail count is reset,
#   the backoff is reset → the supervisor **respawns forever** a child that really
#   is broken.
#
# On an unattended machine the second is especially bad: it turns "give up and
# leave a record" into "silently keep retrying".
#
# This group is **all behavioural tests**: a fake clock forks the two clocks apart,
# then asserts the supervisor's decisions did not follow the wall clock. Just
# checking whether the source says `monotonic` is not enough (same lesson as the
# M4/M5 in the previous section).
# ---------------------------------------------------------------------------


class _Clock:
    """A fake clock: `monotonic` only moves forward, `time` (the wall clock) can be
    jumped on its own.

    **The fake clock must actually move; it must not be pinned to a constant.** The
    measured interval is "now − the value first read", so pinning both ends makes
    the interval always 0, and the test goes green (or red) for the wrong reason
    rather than because of the thing it verifies. `sleep` advances the clock too, or
    the backoff period counts as not having happened in the fake clock.
    """

    def __init__(self, *, wall=1_700_000_000.0, mono=1_000.0):
        self.wall = wall
        self.mono = mono
        self.slept = []

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.advance(seconds)

    def advance(self, seconds):
        """Time really passed — both clocks move together (the normal case)."""
        self.wall += seconds
        self.mono += seconds

    def step_wall(self, seconds):
        """**Move only the wall clock**: an NTP step correction / someone changed
        the clock / a VM snapshot restore."""
        self.wall += seconds


class _NoMoreRounds(Exception):
    """The script is exhausted; use this to break the supervisor's infinite loop.

    Deliberately raises rather than returning an rc: returning an rc would make both
    supervisors treat it as "respawn once more", so the test would not go red but
    **hang** (the lock test above in this file actually hit that).
    """


def _clocked_child(clock, rounds, spawns):
    """Make a fake `stream_child`: advance the clock per `rounds`, return the rc.

    Each entry of `rounds` is `(seconds that really passed this round, extra
    seconds the wall clock jumped, rc)`.
    """
    script = list(rounds)

    def _spy(cmd, _log, **_kwargs):
        spawns.append(cmd)
        if not script:
            raise _NoMoreRounds
        ran_for, wall_jump, rc = script.pop(0)
        clock.advance(ran_for)
        clock.step_wall(wall_jump)
        return rc

    return _spy


def _supervise_with_clock(monkeypatch, tmp_path, rounds, *,
                          rapid_giveup=1, zero_progress_giveup=99):
    """Run `start_webrunner._supervise` under a fake clock, return `(rc, clock,
    spawn count)`.

    `rc is None` means the script is exhausted while the loop still wants another
    round (= the supervisor did **not** give up).
    """
    module, _slot = _launcher_with_pid_file(monkeypatch, tmp_path, None)
    clock = _Clock()
    monkeypatch.setattr(module, "time", clock)
    spawns = []
    monkeypatch.setattr(module, "stream_child",
                        _clocked_child(clock, rounds, spawns))
    try:
        rc = module._supervise(
            ["python", "-u", "webrunner.py"], "selenium", None,
            backoff_min=5.0, backoff_max=300.0, healthy_sec=60.0,
            rapid_threshold_sec=30.0, rapid_giveup=rapid_giveup,
            zero_progress_giveup=zero_progress_giveup)
    except _NoMoreRounds:
        rc = None
    return rc, clock, len(spawns)


def test_the_webrunner_launcher_still_judges_a_run_by_its_real_length(
        monkeypatch, tmp_path):
    """The baseline with no clock jump: a long run counts as healthy, a short one
    as a rapid fail.

    Without this test, pinning `alive_for` to any constant could make the two below
    go green "for the wrong reason".
    """
    rc, _clock, spawns = _supervise_with_clock(
        monkeypatch, tmp_path, [(120.0, 0.0, 1), (0.0, 0.0, 0)])
    assert (rc, spawns) == (0, 2), "lived 120 seconds but was not counted as healthy"

    rc, _clock, spawns = _supervise_with_clock(
        monkeypatch, tmp_path, [(2.0, 0.0, 1)])
    assert (rc, spawns) == (1, 1), "crashed in 2 seconds but was not counted as a rapid fail"


def test_a_backwards_clock_step_does_not_make_a_healthy_run_look_rapid(
        monkeypatch, tmp_path):
    """When the wall clock is turned back, one healthy run must not be misjudged as
    a rapid fail.

    The child really lived 120 seconds (> `healthy_threshold_sec`), but during it
    the wall clock was turned back 10 minutes. Measured with `time.time()`,
    `alive_for` becomes −480 seconds — smaller than any threshold — so
    `rapid_fail_giveup_count=1` fires on the spot and the supervisor **gives up on
    the first round**, while that child is perfectly fine.
    """
    rc, _clock, spawns = _supervise_with_clock(
        monkeypatch, tmp_path, [(120.0, -600.0, 1), (0.0, 0.0, 0)])
    assert spawns == 2, (
        "the supervisor gave up after the wall clock was turned back — one healthy "
        "run counted as a rapid fail. The interval must be measured with "
        "`time.monotonic()`, which is unaffected by clock adjustments.")
    assert rc == 0


def test_a_forwards_clock_step_does_not_reset_the_rapid_fail_counter(
        monkeypatch, tmp_path):
    """When the wall clock is turned forward, one real fast crash must still count
    toward rapid-fail.

    **This is the more dangerous of the two directions**: the child dies in 2
    seconds, but the wall clock jumped forward 10 minutes, so measured with
    `time.time()` you get 602 seconds ≥ `healthy_threshold_sec` → judged healthy →
    the count resets → the supervisor **respawns forever** a child that really is
    broken. On an unattended machine, this turns "give up and leave a record" into
    "silently keep retrying".
    """
    rc, _clock, spawns = _supervise_with_clock(
        monkeypatch, tmp_path, [(2.0, 600.0, 1)])
    assert spawns == 1, (
        "after the wall clock was turned forward, a 2-second crash was treated as a "
        "healthy run and the supervisor respawned another round.")
    assert rc == 1


def _bot_launcher_with_clock(monkeypatch, tmp_path, rounds):
    """Run `start_discord_bot.main()` under a fake clock, return `(seconds slept
    each round, spawn count)`.

    This launcher's loop is `while True` with **no** give-up mechanism (deliberate:
    a brief network outage should not take the bot permanently offline), so the
    observable result is the **backoff sequence** — a healthy round resets it to 5
    seconds, a crashing round doubles it.
    """
    module = _load_launcher("start_discord_bot.py")
    monkeypatch.setattr(module, "LOCK_FILE", tmp_path / "bot.lock")
    monkeypatch.setattr(module, "BOT_LOG", tmp_path / "bot.log")
    monkeypatch.setattr(sys, "argv", ["start_discord_bot.py"])
    clock = _Clock()
    monkeypatch.setattr(module, "time", clock)
    spawns = []
    monkeypatch.setattr(module, "stream_child",
                        _clocked_child(clock, rounds, spawns))
    with pytest.raises(_NoMoreRounds):
        module.main()
    return clock.slept, len(spawns)


def test_the_bot_launcher_still_judges_a_run_by_its_real_length(monkeypatch,
                                                                tmp_path):
    """The baseline (no clock jump): a long second round → backoff resets to 5
    seconds; a short one → doubles to 10."""
    slept, _spawns = _bot_launcher_with_clock(
        monkeypatch, tmp_path, [(2.0, 0.0, 1), (120.0, 0.0, 1)])
    assert slept == [5, 5], f"the backoff was not reset to the minimum after a long run: {slept}"

    slept, _spawns = _bot_launcher_with_clock(
        monkeypatch, tmp_path, [(2.0, 0.0, 1), (2.0, 0.0, 1)])
    assert slept == [5, 10], f"two fast crashes in a row and the backoff did not double: {slept}"


def test_the_bot_launchers_backoff_ignores_a_backwards_clock_step(monkeypatch,
                                                                  tmp_path):
    """A backward wall-clock step must not turn one healthy run into "crashed
    again".

    The second round really lived 120 seconds, but the wall clock was turned back
    10 minutes. Measured with `time.time()` you get −480 seconds → judged unhealthy
    → the backoff keeps growing (10 seconds), when it should have reset to 5.
    """
    slept, _spawns = _bot_launcher_with_clock(
        monkeypatch, tmp_path, [(2.0, 0.0, 1), (120.0, -600.0, 1)])
    assert slept == [5, 5], (
        f"the backoff sequence was affected by the backward wall-clock jump: {slept} "
        "(expected [5, 5]). A run that lived 120 seconds is healthy, regardless of "
        "how the wall clock jumps.")


def test_the_bot_launchers_backoff_ignores_a_forwards_clock_step(monkeypatch,
                                                                 tmp_path):
    """A forward wall-clock step must not launder a fast crash into a healthy run.

    **The dangerous direction**: both rounds crash in 2 seconds, but during the
    second the wall clock jumps forward 10 minutes. Measured with `time.time()` it
    is judged healthy → the backoff resets to 5 seconds, so a bot with a broken
    token keeps reconnecting at a near-fixed 5-second interval — exactly what this
    exponential backoff is meant to avoid.
    """
    slept, _spawns = _bot_launcher_with_clock(
        monkeypatch, tmp_path, [(2.0, 0.0, 1), (2.0, 600.0, 1)])
    assert slept == [5, 10], (
        f"the backoff sequence was affected by the forward wall-clock jump: {slept} "
        "(expected [5, 10]). A forward wall-clock jump launders a fast crash into a "
        "healthy run, and the backoff gets reset because of it.")


# **All** scripts in the repo root, not just the two launchers. The root reason
# this defect could survive is "no scanner covers this directory": the two AST
# guards of the 2026-09-06 project-wide clock scan (`test_bot_helpers`,
# `test_webrunner_shared`) both scan `axiomatic/`, while the launchers live one
# level up. The list is computed from the real repo root at import time, so a repo
# root script added later is **automatically** included, without anyone having to
# remember to come back and add the name.
_ROOT_SCRIPTS = sorted(
    name for name in os.listdir(REPO_ROOT)
    if name.endswith(".py")
    and os.path.isfile(os.path.join(REPO_ROOT, name))
)


@pytest.mark.parametrize("launcher", _ROOT_SCRIPTS)
def test_no_launcher_measures_an_interval_with_the_wall_clock(launcher):
    """A static second cut: no repo root script may contain the shape
    `time.time() - x`.

    The six behavioural tests above pin the **current two** decisions; this one
    blocks the **next** wall-clock interval that gets added — a behavioural test
    cannot see code not yet written. AST is appropriate here, because what to verify
    is precisely a "what the source looks like" property (unlike "does this decision
    hold up against events", which a constant-false condition can bypass and can only
    be pinned behaviourally).

    Writing a timestamp (`time.time()` on its own) is unaffected — only a
    **subtraction** measures an interval.

    The scope is **every** repo root script, not just the two launchers, because the
    root reason this defect could survive is that no scanner covers this directory
    (the two guards of the 2026-09-06 scan both scan `axiomatic/` only). Fix only the
    two and have the guard look only at those two, and the next script placed in the
    repo root repeats the mistake.
    """
    path = os.path.join(REPO_ROOT, launcher)
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    def _is_wall_clock(node):
        return (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "time"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "time")

    offenders = [
        ast.unparse(node) for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)
        and (_is_wall_clock(node.left) or _is_wall_clock(node.right))
    ]
    assert not offenders, (
        f"{launcher} measures an interval with the wall clock: {offenders}. An "
        "in-process interval must use `time.monotonic()` — `time.time()` jumps under "
        "NTP step corrections / clock changes / snapshot restores, and a forward jump "
        "makes the supervisor respawn a broken child forever.")


# ---------------------------------------------------------------------------
# DoD #5's **behavioural** side: actually call `python_command()` (2026-09-08)
#
# The two guards above (`test_the_interpreter_discovery_order_is_intact` /
# `test_both_launchers_discover_the_interpreter_the_same_way`) scan the AST — they
# see "the order the candidates are arranged in" but not "whether that order
# actually takes effect". Before this section, **no test had ever called
# `python_command()`**, and `CLAUDE.md`'s DoD #5 lists it as a hard rule: a fresh
# clone's "install and it runs" rests entirely on this function.
#
# Four ways to get it wrong that the AST cannot catch and only a real run can:
#
#   1. `venv_py.exists()` written backwards (or that path pointing somewhere
#      wrong) — the `return [str(venv_py)]` line is untouched, and the AST sees no
#      difference at all.
#   2. The Windows/POSIX wrong subdirectory (`Scripts` vs `bin`). **This half never
#      executes on this machine**, so it needs testing all the more; swapping
#      `os.name` for a fake reaches it (same lesson as the POSIX flock section
#      above).
#   3. `shutil.which("py")`'s result not used in the return value.
#   4. The `-3` of `["py", "-3"]` dropped — a fresh clone hits the system default
#      interpreter.
#
# **Always monkeypatch `REPO_ROOT` to a temp directory**: this machine's `.venv` is
# the one the production process is using, and the test should not even read it, let
# alone create or delete anything.
# ---------------------------------------------------------------------------


class _FakeOsName:
    """A fake `os` that provides only `name`.

    Deliberately does **not** write `setattr(os, "name", "posix")`: that would
    change the `os.name` the whole process sees, and this file still has daemon
    pump threads running. What is swapped is **the launcher module's own** `os` name
    binding, and every test reloads a fresh module copy via `_load_launcher`, so the
    scope is just this one test.
    """

    def __init__(self, name: str):
        self.name = name


class _FakeShutil:
    """A fake `shutil`: `which` returns a fixed value and records what it was
    asked.

    `asked` is the evidence of short-circuiting — on a `.venv` hit it must be empty
    here, or it means the `.venv` step is not really first (which the AST guard
    cannot see).
    """

    def __init__(self, which_result):
        self._which = which_result
        self.asked: list[str] = []

    def which(self, cmd):
        self.asked.append(cmd)
        return self._which


_VENV_LAYOUT = {
    "nt": (".venv", "Scripts", "python.exe"),
    "posix": (".venv", "bin", "python"),
}
_FAKE_PY_LAUNCHER = os.path.join("C:\\", "Windows", "py.exe")


@contextlib.contextmanager
def _interpreter_probe(launcher, root, *, os_name, venv=None, which=None):
    """Load the launcher, swap `REPO_ROOT` / `os` / `shutil` for fakes, and yield
    the module.

    `root` is always a temp directory — the real repo `.venv` is not touched in the
    slightest. `venv` is the fake interpreter (a relative-path tuple) to be created
    under `root` first, `None` = this clone has no `.venv`. `which` is what
    `shutil.which("py")` should return.

    Use `_Swapped` rather than the `monkeypatch` fixture, for the same reason as the
    POSIX flock section: this file's standalone runner has no fixtures, and with a
    fixture these tests would vanish entirely.
    """
    module = _load_launcher(launcher)
    root = pathlib.Path(root)
    venv_py = None
    if venv is not None:
        venv_py = root.joinpath(*venv)
        venv_py.parent.mkdir(parents=True, exist_ok=True)
        venv_py.write_text("# fake interpreter, only seen by exists(), never executed\n",
                           encoding="utf-8")
    fake_shutil = _FakeShutil(which)
    with contextlib.ExitStack() as stack:
        stack.enter_context(_Swapped(module, "REPO_ROOT", root))
        stack.enter_context(_Swapped(module, "os", _FakeOsName(os_name)))
        stack.enter_context(_Swapped(module, "shutil", fake_shutil))
        yield module, venv_py, fake_shutil


@pytest.mark.parametrize("launcher", _LAUNCHERS)
@pytest.mark.parametrize("os_name", sorted(_VENV_LAYOUT))
def test_a_clone_with_a_venv_runs_that_exact_interpreter(launcher, os_name,
                                                         tmp_path):
    """When `.venv` is present, return **that absolute path**, and pick the right
    subdirectory on both platforms.

    The POSIX half never runs naturally on this Windows machine, so it can only be
    verified by swapping `os.name`. Picking the wrong subdirectory does not report an
    error but **silently degrades**: `.venv/bin/python` does not exist on Windows →
    falls to `py -3` → the production process runs on the system interpreter, and
    dependency versions fork on the spot (exactly the source of the "three
    dependency sets on this machine" trap).
    """
    with _interpreter_probe(launcher, tmp_path, os_name=os_name,
                            venv=_VENV_LAYOUT[os_name],
                            which=_FAKE_PY_LAUNCHER) as (mod, venv_py, sh):
        got = mod.python_command()
    assert got == [str(venv_py)], (
        f"{launcher} with os.name={os_name!r} and `.venv` present returned {got}, "
        f"should be [{str(venv_py)!r}]. DoD #5: the local `.venv` is first.")
    assert os.path.isabs(got[0]), (
        f"{launcher} returned a non-absolute path ({got[0]!r}) — the child's working "
        "directory is the repo root, and a relative path only happens to work; move "
        "it elsewhere and it is not found.")
    assert sh.asked == [], (
        f"{launcher} already found `.venv` but still asked `shutil.which({sh.asked})` "
        "— meaning the `.venv` step is not really before `py -3` (the AST guard "
        "cannot see this, because neither return's literal content changed).")


@pytest.mark.parametrize("launcher", _LAUNCHERS)
@pytest.mark.parametrize("os_name", sorted(_VENV_LAYOUT))
def test_the_other_platforms_venv_layout_is_not_accepted(launcher, os_name,
                                                         tmp_path):
    """When only the **other** platform's `.venv` layout exists, it must not be
    treated as a hit.

    This is the reverse of the previous test. Without it, swapping the two branches'
    subdirectories stays green — because each test is fed only its own layout.
    """
    other = "posix" if os_name == "nt" else "nt"
    with _interpreter_probe(launcher, tmp_path, os_name=os_name,
                            venv=_VENV_LAYOUT[other],
                            which=_FAKE_PY_LAUNCHER) as (mod, venv_py, _sh):
        got = mod.python_command()
    assert got == [_FAKE_PY_LAUNCHER, "-3"], (
        f"{launcher} under os.name={os_name!r} treated the {other} layout "
        f"({venv_py}) as a usable interpreter and returned {got}. The two branches' "
        "subdirectories were picked backwards.")


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_a_fresh_clone_without_a_venv_falls_back_to_the_py_launcher(launcher,
                                                                    tmp_path):
    """A fresh clone with no `.venv` must fall to `py -3`, and `-3` must not be
    dropped.

    `-3` is the only truly fatal part of this step: without it, `py` picks the
    system default version — possibly Python 2, possibly another 3.x — so someone who
    has not even run `py -3 -m venv .venv` hits an incomprehensible import failure on
    their first launch. The AST test only confirms "some return contains `-3`", not
    that it is actually returned.
    """
    with _interpreter_probe(launcher, tmp_path, os_name=os.name, venv=None,
                            which=_FAKE_PY_LAUNCHER) as (mod, _venv, sh):
        got = mod.python_command()
    assert got == [_FAKE_PY_LAUNCHER, "-3"], (
        f"{launcher} with no `.venv` returned {got}, should be "
        f"[{_FAKE_PY_LAUNCHER!r}, '-3'].")
    assert sh.asked == ["py"], (
        f"{launcher} did not ask for `py` (it asked {sh.asked}) — DoD #5 names the "
        "Windows py launcher specifically, which is what avoids the Microsoft Store "
        "stub.")


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_without_a_venv_or_a_py_launcher_it_uses_the_current_interpreter(
        launcher, tmp_path):
    """When neither is present, use the interpreter that started this launcher —
    the last fallback, which must not return empty."""
    with _interpreter_probe(launcher, tmp_path, os_name=os.name, venv=None,
                            which=None) as (mod, _venv, _sh):
        got = mod.python_command()
    assert got == [sys.executable], (
        f"{launcher} on a machine with neither `.venv` nor `py` returned {got}, "
        f"should be [{sys.executable!r}]. This is the fallback step, and getting it "
        "wrong means it cannot start at all.")


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_the_venv_beats_the_py_launcher_when_both_are_available(launcher,
                                                                tmp_path):
    """**The order really is the order**: when both are present, `.venv` must be
    chosen.

    This dev machine is exactly "both present", so this test is the path the
    production process actually takes every day. Getting it wrong makes the bot and
    webrunner run on the system interpreter — the symptom is not a startup failure
    but a quietly swapped set of dependency versions.
    """
    layout = _VENV_LAYOUT["nt" if os.name == "nt" else "posix"]
    with _interpreter_probe(launcher, tmp_path, os_name=os.name, venv=layout,
                            which=_FAKE_PY_LAUNCHER) as (mod, venv_py, sh):
        got = mod.python_command()
    assert got == [str(venv_py)], (
        f"{launcher} with both `.venv` and `py` present chose {got}, should choose "
        f"`.venv` ({venv_py}).")
    assert sh.asked == [], "after choosing `.venv` it should not go on to ask for `py`"


# ---------------------------------------------------------------------------
# There are **three** copies of `python_command()`; the third is deliberately
# different
# ---------------------------------------------------------------------------
# The two launcher copies must be identical verbatim (the AST guard above watches
# this). `install_autostart.py`'s third copy is **deliberately different**, for the
# reason in its own docstring: when the task scheduler runs, PATH and environment
# variables differ from an interactive shell, so it hardcodes an absolute path
# rather than going through `py -3`; it also deliberately avoids `pythonw.exe` (a
# scheduler-launched `pythonw` does not inherit the standard handles, `sys.stdout`
# is `None`, and this whole stack `print()`s everywhere, so the first line blows up
# as `AttributeError`).
#
# Without the two tests below, a reader of "the two launchers must be identical
# verbatim" easily treats the third copy as the **missing** one and "unifies" it out
# of hand — a change that looks entirely harmless, at the cost of the autostart set
# never starting again, with no error message.
# **The two lists must line up (fixed 2026-09-11).** `_LAUNCHERS` (top of this
# file) drives a dozen-plus parametrized guards — discovery order, verbatim
# identity, single-instance lock, degraded wording, monotonic clock,
# `stream_child`… — while the table below only answers "has this copy been claimed".
# Before this the two were **unrelated**: adding a `start_thing.py` made the table
# below go red, and the cheapest fix was to add one string; after that everything is
# green while the new launcher's discovery order has **not one guard**. The red
# light even taught "registering counts as handled", which is worse than not having
# that light. So the "launcher" category is now **computed from `_LAUNCHERS`**, not
# copied separately — classifying is covering.
#
# The exemption list: **only for the copies that deliberately do the opposite**,
# not an escape hatch for new launchers. A stale exemption is fail-open — after a
# file is renamed or that `python_command()` is removed, the string matches nothing
# any more, the guard runs, every test is green, and the checked set has quietly
# lost one (the same shape `CLAUDE.md` records for `_OWNER_ONLY_SLASH`).
_PYTHON_COMMAND_EXEMPT = {
    "install_autostart.py":
        "Task-scheduler-specific, **deliberately divergent**. Does not go through "
        "`py -3`: `py.exe` is a launcher, and `-3` only consults the registry, "
        "`PY_PYTHON`, `py.ini` and the shebang at run time; the environment a login "
        "task gets differs from an interactive shell, and it will definitely not "
        "resolve to `.venv`. It also only looks in `.venv/Scripts/`, because "
        "`main()` finishes outright on non-Windows. See the function's docstring "
        "for the full reasoning.",
}


def _root_python_command_files() -> set:
    """The repo root files that define `python_command()` at **module level**,
    computed by AST.

    Look at `tree.body` only, not `ast.walk`: a same-named definition nested in a
    function or class is not the DoD #5 handoff point, and including it only creates
    false positives.
    """
    found = set()
    for name in _ROOT_SCRIPTS:
        tree = ast.parse(
            pathlib.Path(REPO_ROOT, name).read_text(encoding="utf-8"))
        if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == "python_command"
               for node in tree.body):
            found.add(name)
    return found


def _python_command_registration_errors(found, launchers, exempt, *,
                                        min_found=3, min_launchers=2) -> list:
    """The reconciliation criteria for the classification table. A **pure
    function** — same reason as `test_verify_browser._exemption_errors`: the current
    state is clean, so deleting the assertion in the main test would not go red on
    its own, and the teeth must grow where a synthetic corpus can ask them.
    """
    errors = []
    # (A) Population floor. An empty `found` makes (C)(D)(E) all **vacuously
    #     true**, with output identical to "everything compliant"; `_ROOT_SCRIPTS`
    #     scanning the wrong directory is exactly this outcome.
    if len(found) < min_found:
        errors.append(
            f"the repo root scan found only {len(found)} `python_command()` copies "
            f"(floor {min_found}): {sorted(found)}. An empty checked set makes every "
            "reconciliation below pass vacuously, with output indistinguishable from "
            "'everything compliant'.")
    # (B) `_LAUNCHERS`'s floor. It is the parameter source for a dozen-plus
    #     parametrized guards, and pytest treats an **empty** parameter set as
    #     skipped, not error — emptying it silently switches those dozen off.
    if len(launchers) < min_launchers:
        errors.append(
            f"`_LAUNCHERS` has only {len(launchers)} left (floor {min_launchers}): "
            f"{sorted(launchers)}. An empty parameter set is just a few fewer output "
            "lines in pytest, not red.")
    # (C) Every copy must be classified. Adding a launcher goes red here first.
    unclaimed = set(found) - set(launchers) - set(exempt)
    if unclaimed:
        errors.append(
            f"these `python_command()` copies are unclassified: {sorted(unclaimed)}. "
            "Each is either a launcher (add it to `_LAUNCHERS`, and the dozen-plus "
            "guards cover it automatically, checking discovery order and verbatim "
            "identity) or deliberately divergent (add it to `_PYTHON_COMMAND_EXEMPT` "
            "with a reason). **Adding one line of registration is not enough** — that "
            "is exactly what this blocks.")
    # (D) The reverse: a name in `_LAUNCHERS` must still really have a
    #     `python_command()`.
    stale = set(launchers) - set(found)
    if stale:
        errors.append(
            f"the {sorted(stale)} in `_LAUNCHERS` are no longer where "
            f"`python_command()` lives (renamed file, or the function removed). "
            f"Currently scanned: {sorted(found)}")
    # (E) The reverse: the fail-open side of the exemption list, plus the reason
    #     must not be perfunctory.
    for name, reason in exempt.items():
        if name not in found:
            errors.append(
                f"{name!r} in the exemption list no longer has a `python_command()` "
                f"(renamed file, or the function removed). Currently scanned: "
                f"{sorted(found)}")
        if len(str(reason).strip()) < 20:
            errors.append(f"{name}'s exemption gives no specific enough reason: {reason!r}")
    # (F) The same file must not be listed on both sides — those are two opposite
    #     dispositions, and when they overlap "should it be identical verbatim" has
    #     no answer; the test runs on the `_LAUNCHERS` side, so the exemption is
    #     silently ignored.
    both = set(launchers) & set(exempt)
    if both:
        errors.append(
            f"{sorted(both)} is listed in both `_LAUNCHERS` and "
            "`_PYTHON_COMMAND_EXEMPT`. Those are two opposite dispositions (must be "
            "identical verbatim / deliberately divergent), pick one side.")
    return errors


def test_every_copy_of_python_command_is_accounted_for():
    """Every `python_command()` in the repo root must be **classified**, not just
    registered.

    Fail-closed: a fourth copy makes this go red, and the message forces the writer
    to answer "who should it be identical to". Without this layer, an unknown copy
    can live a long time carrying its own discovery order.

    **Strengthened 2026-09-11.** It used to ask "is this name in the registration
    table", which one string could satisfy — a string is prose, and nothing about it
    tells whether that file should go into `_LAUNCHERS`. So "add one line of
    registration" looked like a complete fix, the new launcher's discovery order was
    watched by no guard, and the red light itself endorsed that wrong fix. Now the
    launcher category is computed from `_LAUNCHERS`, so **classifying is covering**.
    """
    problems = _python_command_registration_errors(
        _root_python_command_files(), _LAUNCHERS, _PYTHON_COMMAND_EXEMPT)
    assert not problems, "\n".join(problems)


def test_the_python_command_registration_bites_on_a_synthetic_corpus():
    """The control: the real data is clean, so the test above **would not go red
    on deleting any single branch**.

    Each branch gets a corpus that **violates only itself**. Using one shared corpus
    would let two branches mask each other — delete one and the other still flags
    that corpus as a problem, and the mutation survives (the same shape was just hit
    on `_collect_codex_images` this round).
    """
    clean_found = {"a.py", "b.py", "x.py"}
    clean_launchers = ("a.py", "b.py")
    clean_exempt = {"x.py": "deliberately divergent, and this reason is written long enough to pass the length floor."}

    def _errors(found=None, launchers=None, exempt=None):
        return _python_command_registration_errors(
            clean_found if found is None else found,
            clean_launchers if launchers is None else launchers,
            clean_exempt if exempt is None else exempt)

    assert _errors() == [], (
        f"the control corpus should itself be clean, or none of the branches below "
        f"can prove they bit on their own: {_errors()}")

    cases = [
        ("A population floor", dict(found={"a.py", "b.py"}, exempt={}), "floor 3"),
        ("B `_LAUNCHERS` floor",
         dict(found={"a.py", "x.py", "y.py"}, launchers=("a.py",),
              exempt={"x.py": clean_exempt["x.py"],
                      "y.py": clean_exempt["x.py"]}), "floor 2"),
        ("C new copy is unclassified",
         dict(found=clean_found | {"start_thing.py"}), "start_thing.py"),
        ("D `_LAUNCHERS` has a stale name",
         dict(launchers=clean_launchers + ("gone.py",)), "gone.py"),
        ("E1 exemption list has a stale name",
         dict(exempt={**clean_exempt, "gone.py": clean_exempt["x.py"]}),
         "gone.py"),
        ("E2 the exemption reason is too perfunctory", dict(exempt={"x.py": "too short"}), "specific enough reason"),
        ("F the same file is listed on both sides",
         dict(exempt={"a.py": clean_exempt["x.py"],
                      "x.py": clean_exempt["x.py"]}), "pick one side"),
    ]
    for label, corpus, needle in cases:
        got = _errors(**corpus)
        assert len(got) == 1, (
            f"{label}: this corpus should trigger **only** one, got {len(got)}. Two "
            f"or more means the corpus is not isolated, and deleting one branch would "
            f"still be masked by another.\n{got}")
        assert needle in got[0], f"{label}: the message does not mention {needle!r}: {got[0]}"


def test_the_autostart_copy_is_deliberately_different_not_a_missed_one():
    """The third copy **must not** be "unified" into the launcher one.

    Asserting non-equality looks counterintuitive, but that is precisely the
    property to protect: putting `py -3` back onto the scheduler path, where the task
    scheduler's PATH differs, might find another interpreter or none at all — and
    that failure only surfaces on the next reboot, with nobody watching.
    """
    autostart = ast.unparse(_python_command_node("install_autostart.py"))
    launcher = ast.unparse(_python_command_node("start_discord_bot.py"))
    assert autostart != launcher, (
        "`install_autostart.python_command()` was changed to match the launcher "
        "copy. That copy is **deliberately divergent**, not a missed copy: the "
        "scheduler's PATH differs from an interactive shell, so it hardcodes the "
        "`.venv/Scripts/` absolute path rather than going through `py -3`. To change "
        "it, read its docstring first, then update `_PYTHON_COMMAND_EXEMPT`'s note.")
    assert "'-3'" not in autostart and '"-3"' not in autostart, (
        f"`install_autostart.python_command()` grew a `py -3`: {autostart}")


# ---------------------------------------------------------------------------
# The two launchers' wrap-up and failure paths (added 2026-09-08)
#
# Measuring coverage found: `_supervisor.py` is 100%, but the two launchers are
# not — the whole missing swath is paths that only run "after something has gone
# wrong". Same criterion as the `_supervisor.py` section: the supervisor's only
# reason to exist is to hold up when other things break, so those branches are its
# most central responsibility.
#
# Every test in this section **never actually starts a bot or webrunner**:
# `stream_child` is always swapped for a spy or an interceptor that raises. A
# webrunner on startup unconditionally nuclear-sweeps all Chrome, and this machine
# usually has a multi-day unattended batch using it.
#
# **The fake child always has a cap**: over the expected number of rounds it throws
# `_NoMoreRounds` to break the loop. Both supervisors' loops are `while True`, and
# the fake clock's `sleep` does not really wait, so the symptom of "this early-exit
# path was broken" is the **test hanging**, not going red — mutation testing hit it
# for real: after changing the `child_exit_is_fatal` branch to constant-false, the
# test was not red but spun all the way to `attempt 7088727` before being killed by
# timeout. A hung test is worse than a red one: red points at the problem, hung just
# leaves the whole run stuck there.
# ---------------------------------------------------------------------------

def _expect_no_extra_round(call, what):
    """Run `call()`, and if the supervisor spun another round, fail with a legible
    message.

    The fake child's cap is made with `_NoMoreRounds` (see the start of this
    section), and a bare `_NoMoreRounds` only says "called too many times", not why
    that is wrong. Translate it back to the property being protected, so the next
    person seeing the red does not have to go read the test's implementation.
    """
    try:
        return call()
    except _NoMoreRounds:
        pytest.fail(
            f"{what} — but the supervisor respawned another round. This early-exit "
            "path must end the loop on the spot: with normal backoff it keeps "
            "respawning a child doomed to fail the same way, and rapid-fail giveup "
            "only looks at 'how long it ran' and cannot see this.")


BOT_LAUNCHER_NAME = "start_discord_bot.py"


class _SpyLock:
    """A fake single-instance lock: records how many times it was released.

    Swapping the real one is a **safety requirement**: the real one touches the repo
    root's `.discord_bot_supervisor.lock` / `.webrunner_supervisor.lock`, which the
    production supervisors are holding.
    """

    def __init__(self, *, degraded=False):
        self.degraded = degraded
        self.releases = 0

    def release(self):
        self.releases += 1


class _CtrlCOnSleep(_Clock):
    """Ctrl+C partway through the backoff sleep."""

    def sleep(self, seconds):
        self.slept.append(seconds)
        raise KeyboardInterrupt


def _bot_launcher(monkeypatch, tmp_path, child, *, lock=None, clock=None,
                  log_path=None):
    """Set up `start_discord_bot` on stubs, return `(module, lock, clock)`.

    `child` is the fake `stream_child`. The caller decides when to call
    `module.main()`.
    """
    module = _load_launcher(BOT_LAUNCHER_NAME)
    monkeypatch.setattr(module, "LOCK_FILE", tmp_path / "bot.lock")
    monkeypatch.setattr(module, "BOT_LOG",
                        log_path if log_path is not None
                        else tmp_path / "bot.log")
    monkeypatch.setattr(sys, "argv", [BOT_LAUNCHER_NAME])
    lock = _SpyLock() if lock is None else lock
    monkeypatch.setattr(module, "acquire_single_instance_lock",
                        lambda _path: lock)
    clock = _Clock() if clock is None else clock
    monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(module, "stream_child", child)
    return module, lock, clock


def test_the_bot_launcher_stops_instead_of_respawning_a_doomed_child(
        monkeypatch, tmp_path):
    """When the bot reports "another instance is already running", the launcher
    must wrap up, not back off and respawn.

    This branch's comment states the reason itself: the bot body has its own lock,
    and when blocked it exits cleanly within a second every time. With normal
    backoff this loop would respawn, every 5–300 seconds, a child **doomed to be
    blocked by the same lock**, and rapid-fail giveup only looks at "how long it ran"
    and cannot see this — it would spin forever.
    """
    spawns = []

    def _child(cmd, _log, **_kwargs):
        spawns.append(cmd)
        if len(spawns) > 1:
            raise _NoMoreRounds          # see "the fake child always has a cap" below
        return RC_ALREADY_RUNNING

    log_path = tmp_path / "bot.log"
    module, lock, clock = _bot_launcher(monkeypatch, tmp_path, _child,
                                        log_path=log_path)
    rc = _expect_no_extra_round(module.main,
                                "the bot reports another instance is already running")

    assert rc == RC_ALREADY_RUNNING, (
        f"the fatal rc should be passed straight out (so an outer scheduler can see "
        f"it too), got {rc}")
    assert len(spawns) == 1, (
        f"respawned {len(spawns)} times. This kind of failure retry never succeeds, "
        "and the loop must stop on the spot.")
    assert clock.slept == [], (
        f"it still slept the backoff {clock.slept} — meaning it took the ordinary "
        "crash path, not this one.")
    assert lock.releases == 1, f"the lock was not released on wrap-up (releases={lock.releases})"
    # The substring below is the launcher's Chinese "another instance" line, which
    # lives in start_discord_bot.py (out of scope here), so it is intentionally left
    # untranslated.
    assert "另一個實例" in log_path.read_text(encoding="utf-8"), (
        "it wrapped up but did not write the reason to the log. On this path the "
        "user just sees the launcher exit directly, and without that line of "
        "explanation nobody knows which process to end. (If the wording changes, "
        "update this test.)")


def test_the_bot_launcher_holds_the_lock_until_the_loop_is_over(monkeypatch,
                                                                tmp_path):
    """The lock must be held the **whole time**, and always releasable on the way
    out (the one in `finally`).

    Two directions pinned together: releasing it partway through the loop lets a
    second supervisor start on the spot; and if `release()` is not in `finally`, any
    exception thrown outward would skip it.
    """
    held_during_run = []

    def _child(cmd, _log, **_kwargs):
        held_during_run.append(lock.releases)
        raise _NoMoreRounds

    lock = _SpyLock()
    module, lock, _clock = _bot_launcher(monkeypatch, tmp_path, _child,
                                         lock=lock)
    with pytest.raises(_NoMoreRounds):
        module.main()

    assert held_during_run == [0], (
        "the lock was released while the child was still running — during that "
        "window a second supervisor can start, and both would 'look normal'.")
    assert lock.releases == 1, (
        f"an exception thrown outward skipped `lock.release()` (releases={lock.releases}). "
        "It must be in `finally`.")


def test_a_ctrl_c_while_the_bot_runs_exits_cleanly_without_respawning(
        monkeypatch, tmp_path):
    """Ctrl+C is a **clean exit** (rc=0), not a crash — no respawning another
    round."""
    spawns = []

    def _child(cmd, _log, **_kwargs):
        spawns.append(cmd)
        if len(spawns) > 1:
            raise _NoMoreRounds          # see "the fake child always has a cap" below
        raise KeyboardInterrupt

    module, lock, clock = _bot_launcher(monkeypatch, tmp_path, _child)
    rc = _expect_no_extra_round(module.main, "Ctrl+C received while the child runs")

    assert rc == 0, f"Ctrl+C should return rc=0, got {rc}"
    assert len(spawns) == 1, "respawned another round after Ctrl+C"
    assert clock.slept == [], "still slept the backoff after Ctrl+C"
    assert lock.releases == 1, "the lock was not released after Ctrl+C"


def test_a_ctrl_c_during_the_backoff_sleep_also_exits_cleanly(monkeypatch,
                                                              tmp_path):
    """The minutes of waiting for backoff are exactly when Ctrl+C is most likely —
    that path needs its own exit.

    This is the more easily missed of the two Ctrl+C exits: the loop actually spends
    most of its time here.
    """
    spawns = []

    def _child(cmd, _log, **_kwargs):
        spawns.append(cmd)
        if len(spawns) > 1:
            raise _NoMoreRounds          # see "the fake child always has a cap" below
        return 1

    module, lock, clock = _bot_launcher(monkeypatch, tmp_path, _child,
                                        clock=_CtrlCOnSleep())
    rc = _expect_no_extra_round(module.main, "Ctrl+C received while waiting for backoff")

    assert rc == 0, f"a Ctrl+C partway through the sleep should return rc=0, got {rc}"
    assert clock.slept == [5], (
        f"the first crash should sleep the minimum backoff of 5 seconds (got {clock.slept})")
    assert len(spawns) == 1, "respawned another round after Ctrl+C"
    assert lock.releases == 1


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_a_log_file_that_will_not_open_does_not_stop_the_launcher(launcher,
                                                                  tmp_path,
                                                                  monkeypatch):
    """A log file that will not open can only degrade to "console only"; it **must
    not** stop startup.

    The criterion is in both launchers' comments: the supervisor should not refuse
    to start the bot / the whole image batch over an ancillary feature. Using a
    **directory** as the log path to produce a real `OSError` (measured: Windows
    gives `PermissionError`) is closer to a real failure than swapping `open`.
    """
    as_a_log = tmp_path / "log_is_a_directory"
    as_a_log.mkdir()

    spawns = []

    def _child(_cmd, log, **_kwargs):
        spawns.append(log)
        raise _NoMoreRounds

    module = _load_launcher(launcher)
    monkeypatch.setattr(module, "LOCK_FILE", tmp_path / "test.lock")
    monkeypatch.setattr(module, "acquire_single_instance_lock",
                        lambda _path: _SpyLock())
    monkeypatch.setattr(sys, "argv", [launcher])
    monkeypatch.setattr(
        module,
        "WEBRUNNER_LOG" if "webrunner" in launcher else "BOT_LOG",
        as_a_log)
    monkeypatch.setattr(module, "stream_child", _child)
    if hasattr(module, "_chrome_slot"):
        monkeypatch.setattr(module, "_chrome_slot", _FakeSlot())
        monkeypatch.setattr(module, "WEBRUNNER_PID_FILE",
                            tmp_path / "webrunner.pid")

    with pytest.raises(_NoMoreRounds):
        module.main()

    assert spawns == [None], (
        f"{launcher} did not degrade to 'console only' when the log file would not "
        f"open (the log passed to stream_child was {spawns}) — it should start the "
        "child as usual, just without writing a file.")


# ---------------------------------------------------------------------------
# start_webrunner: the Chrome slot's short critical section and early-exit paths
# ---------------------------------------------------------------------------


class _OrderingSlot(_FakeSlot):
    """`_FakeSlot` plus one thing: on each `release`, record the pid file's content
    at that moment.

    This is the only way to test the "**write the pid before releasing the slot**"
    ordering contract — with both actions done, only the order reversed, nothing is
    distinguishable afterward, but in that one instant the slot is empty and the pid
    is not yet written, and if the verifier takes the slot right then it judges
    nobody is running and opens a second Chrome stack.
    """

    def __init__(self, pid_file, *, acquired=True, pid_alive=True):
        super().__init__(pid_alive=pid_alive)
        self._pid_file = pathlib.Path(pid_file)
        self._acquired = acquired
        self.pid_at_release: list[str | None] = []

    def acquire(self, owner, *, timeout=0.0, label=""):
        return self._acquired

    def release(self, owner):
        super().release(owner)
        self.pid_at_release.append(
            self._pid_file.read_text(encoding="utf-8")
            if self._pid_file.exists() else None)


class _SpawnedProc:
    """The `Popen` that `stream_child` hands to `on_spawn` only has `.pid` read.

    Deliberately not named `_FakeProc` — the reaping section above already has a
    class by that name, and shadowing it would make those five tests go red with
    `AttributeError` (hit for real).
    """

    def __init__(self, pid: int):
        self.pid = pid


def _webrunner_module(monkeypatch, tmp_path, *, slot=None):
    """Load `start_webrunner`, pointing both the pid file and the Chrome slot at a
    temp directory.

    **Never touch the production `webrunner.pid` and `chrome_slot.lock`**: this
    machine often has a batch that has run for tens of hours using them.
    """
    module = _load_launcher(WEBRUNNER_LAUNCHER)
    pid_file = tmp_path / "webrunner.pid"
    monkeypatch.setattr(module, "WEBRUNNER_PID_FILE", pid_file)
    slot = _OrderingSlot(pid_file) if slot is None else slot
    monkeypatch.setattr(module, "_chrome_slot", slot)
    return module, slot, pid_file


def _supervise_once(module, log=None, **overrides):
    params = dict(backoff_min=5.0, backoff_max=300.0, healthy_sec=60.0,
                  rapid_threshold_sec=30.0, rapid_giveup=3,
                  zero_progress_giveup=3)
    params.update(overrides)
    return module._supervise(["python", "-u", "webrunner.py"], "selenium", log,
                             **params)


def test_the_pid_is_on_disk_before_the_chrome_slot_is_released(monkeypatch,
                                                               tmp_path):
    """The short critical section's ordering contract: **take slot → spawn → write
    pid → release slot**.

    Writing it in reverse (release the slot before writing the pid) leaves a window:
    the slot is empty and the pid is not yet written, and if the verifier takes the
    slot in that instant it judges "no batch is running", opens a second Chrome stack
    against the same login profile, and is then swept by the webrunner's per-character
    restart. Afterward both records look normal.
    """
    module, slot, pid_file = _webrunner_module(monkeypatch, tmp_path)
    rounds = []

    def _child(_cmd, _log, *, on_spawn=None, **_kwargs):
        rounds.append(1)
        if len(rounds) > 1:
            raise _NoMoreRounds          # see "the fake child always has a cap" below
        on_spawn(_SpawnedProc(4242))
        return 0

    monkeypatch.setattr(module, "stream_child", _child)
    assert _supervise_once(module) == 0

    assert slot.pid_at_release, "the slot was never released — it stays taken until the staleness timeout"
    assert slot.pid_at_release[0] == "4242", (
        f"at the moment the slot was released the pid file content was "
        f"{slot.pid_at_release[0]!r}, should already be '4242'. The order is "
        "reversed: the slot is empty and the pid not yet written, and the verifier "
        "would open a second Chrome stack in that window.")


def test_the_liveness_signal_is_taken_back_when_the_child_is_gone(monkeypatch,
                                                                  tmp_path):
    """After the child exits, `webrunner.pid` must be reclaimed (the step in
    `finally`).

    Missing it, that file keeps declaring "the batch is still running" and the
    verifier stands aside forever — and the symptom is "the verification script keeps
    SKIPping", which nobody would look here for.
    """
    module, _slot, pid_file = _webrunner_module(monkeypatch, tmp_path)
    rounds = []

    def _child(_cmd, _log, *, on_spawn=None, **_kwargs):
        rounds.append(1)
        if len(rounds) > 1:
            raise _NoMoreRounds          # see "the fake child always has a cap" below
        on_spawn(_SpawnedProc(4242))
        assert pid_file.exists(), "the pid file should be there while the child runs"
        return 0

    monkeypatch.setattr(module, "stream_child", _child)
    assert _supervise_once(module) == 0
    assert not pid_file.exists(), (
        "the child has exited but `webrunner.pid` still remains — the verifier would "
        "forever think a batch is running.")


def test_a_chrome_slot_that_never_frees_up_refuses_to_spawn(monkeypatch,
                                                            tmp_path):
    """If the slot never frees up, wrap up with rc=1, **not** a retry loop.

    Not getting it by the timeout (300s > the verification script's own 240s budget)
    means it is not "just scheduled in the middle of verification" but someone stuck
    — that needs a human to look. The important thing is that this path **touches
    nothing**: no sweep, no spawn.
    """
    pid_file = tmp_path / "webrunner.pid"
    slot = _OrderingSlot(pid_file, acquired=False)
    module, _slot, _pid = _webrunner_module(monkeypatch, tmp_path, slot=slot)

    def _child(*_args, **_kwargs):
        raise AssertionError("spawned a webrunner before even getting the slot")

    monkeypatch.setattr(module, "stream_child", _child)
    assert _supervise_once(module) == 1
    assert slot.released == [], (
        f"the slot was never acquired but was still released: {slot.released}. "
        "`release` only deletes the file while this process still holds it, but here "
        "it should not even be called — what it releases could be someone else's slot.")
    assert not pid_file.exists(), "no spawn but a liveness signal was written"


def test_a_blocked_generation_stops_the_launcher_instead_of_respawning(
        monkeypatch, tmp_path):
    """rc=4 (the site blocked generation) must stop on the spot, not back off and
    respawn.

    Respawning would only see the same dialog, and re-run login + setup every round.
    This is unrelated to rapid-fail: the blocked round can run a long time, and
    rapid-fail never fires.
    """
    module, _slot, _pid = _webrunner_module(monkeypatch, tmp_path)
    clock = _Clock()
    monkeypatch.setattr(module, "time", clock)
    spawns = []

    def _child(cmd, _log, *, on_spawn=None, **_kwargs):
        spawns.append(cmd)
        if len(spawns) > 1:
            raise _NoMoreRounds          # see "the fake child always has a cap" below
        on_spawn(_SpawnedProc(4242))
        clock.advance(120.0)
        return 4

    monkeypatch.setattr(module, "stream_child", _child)
    assert _expect_no_extra_round(
        lambda: _supervise_once(module), "the site blocked generation (rc=4)") == 1
    assert len(spawns) == 1, (
        f"generation was blocked but it still respawned (spawned {len(spawns)} times) "
        "— every round re-runs login and setup, then sees the same dialog.")
    assert clock.slept == [], "still slept the backoff after being blocked"


def test_a_ctrl_c_during_a_batch_releases_the_slot_and_the_pid(monkeypatch,
                                                               tmp_path):
    """Ctrl+C returns rc=0, and both wrap-up steps in `finally` must complete.

    Missing either, the next start is blocked by what the last one left behind: the
    slot is not reclaimed until the 600-second staleness backstop, and the pid file
    keeps the verifier standing aside.
    """
    module, slot, pid_file = _webrunner_module(monkeypatch, tmp_path)

    def _child(_cmd, _log, *, on_spawn=None, **_kwargs):
        on_spawn(_SpawnedProc(4242))
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "stream_child", _child)
    assert _supervise_once(module) == 0, "Ctrl+C should return rc=0"
    assert not pid_file.exists(), "the liveness signal was not reclaimed after Ctrl+C"
    assert slot.released == [module.SLOT_OWNER] * 2, (
        f"the slot's release count is wrong: {slot.released} (`_on_spawn` once, "
        "`finally` once; `release` only deletes the file while this process still "
        "holds it, so repeated calls are safe)")


def test_a_ctrl_c_during_the_webrunners_backoff_also_exits_cleanly(monkeypatch,
                                                                   tmp_path):
    """The webrunner side's second Ctrl+C exit.

    Backoff is up to 300 seconds, so "where the loop is stopped the moment Ctrl+C is
    pressed" is most likely here, not mid-child. Without this exit, KeyboardInterrupt
    blows straight out as a traceback while `finally` has already run — it looks
    broken, but really nobody caught it.
    """
    module, slot, pid_file = _webrunner_module(monkeypatch, tmp_path)
    clock = _CtrlCOnSleep()
    monkeypatch.setattr(module, "time", clock)
    spawns = []

    def _child(cmd, _log, *, on_spawn=None, **_kwargs):
        spawns.append(cmd)
        if len(spawns) > 1:
            raise _NoMoreRounds          # see "the fake child always has a cap" below
        on_spawn(_SpawnedProc(4242))
        clock.advance(2.0)
        return 1

    monkeypatch.setattr(module, "stream_child", _child)
    assert _expect_no_extra_round(
        lambda: _supervise_once(module),
        "Ctrl+C received while waiting for backoff") == 0, "a Ctrl+C partway through the sleep should return rc=0"
    assert clock.slept == [5.0], (
        f"the first crash should sleep the minimum backoff of 5 seconds (got {clock.slept})")
    assert len(spawns) == 1, "respawned another round after Ctrl+C"
    assert not pid_file.exists(), "the liveness signal was not reclaimed after Ctrl+C"


def test_the_slow_zero_progress_gate_stops_what_rapid_fail_cannot(monkeypatch,
                                                                  tmp_path):
    """Consecutive "ran a long time but saved not a single image" must give up —
    rapid-fail cannot catch this kind.

    Both halves tested together, because looking at either half alone can be fooled
    by a wrong implementation:

    * First half: every round lives 40 seconds (> `rapid_fail_threshold_sec`, so
      rapid-fail resets each round and never fires) and returns rc=3, and it must
      give up after two rounds.
    * Second half: the same input, only with `zero_progress_giveup` raised, must
      keep respawning — otherwise "give up forever" would make the first half green
      too, which is worse than the original defect.
    """
    rounds = [(40.0, 0.0, RC_ZERO_PROGRESS)] * 5
    rc, clock, spawns = _supervise_with_clock(
        monkeypatch, tmp_path, list(rounds), rapid_giveup=99,
        zero_progress_giveup=2)
    assert (rc, spawns) == (1, 2), (
        f"after two consecutive rounds of zero output it should give up (rc=1, "
        f"spawned 2 times), got rc={rc}, spawned {spawns} times. When generation is "
        "blocked, every round runs to consecutive_fail_abort before ending, far past "
        "rapid_fail_threshold_sec — that gate never fires.")
    assert clock.slept, "the backoff of the round before giving up was not slept"

    rc, _clock, spawns = _supervise_with_clock(
        monkeypatch, tmp_path, list(rounds), rapid_giveup=99,
        zero_progress_giveup=99)
    assert (rc, spawns) == (None, 6), (
        f"with the threshold raised it should keep respawning (rc={rc}, spawned "
        f"{spawns} times). Without this half, writing the gate as 'give up forever' "
        "would also be all green.")


def test_a_good_run_clears_the_zero_progress_counter(monkeypatch, tmp_path):
    """The count requires **consecutive** — one round with output in the middle
    resets it.

    Without resetting, a machine that has run for days with an occasional single
    round of zero output slowly accumulates to the threshold and then stops while
    everything is perfectly normal.
    """
    rc, _clock, spawns = _supervise_with_clock(
        monkeypatch, tmp_path,
        [(40.0, 0.0, RC_ZERO_PROGRESS), (40.0, 0.0, 1),
         (40.0, 0.0, RC_ZERO_PROGRESS), (40.0, 0.0, 0)],
        rapid_giveup=99, zero_progress_giveup=2)
    assert (rc, spawns) == (0, 4), (
        f"the middle round was not zero output but the count did not reset (rc={rc}, "
        f"spawned {spawns} times).")


def test_a_backoff_config_with_max_below_min_is_refused_before_anything_spawns(
        monkeypatch, tmp_path):
    """`max < min` must be blocked at startup, not left to blow up on the first
    crash.

    `_bot_config._coerce_supervisor` only guarantees each value is > 0 on its own, so
    a config like this loads and starts, then on the **first crash** makes
    `restart_backoff` throw `ValueError` and take the whole supervisor down — exactly
    the moment it is supposed to take over. The message must name both keys, or the
    user only knows "the config is broken" without knowing which pair.
    """
    module = _load_launcher(WEBRUNNER_LAUNCHER)
    monkeypatch.setattr(sys, "argv", [WEBRUNNER_LAUNCHER])
    monkeypatch.setattr(module, "LOCK_FILE", tmp_path / "test.lock")
    monkeypatch.setattr(module, "WEBRUNNER_LOG", tmp_path / "webrunner.log")
    monkeypatch.setattr(module, "WEBRUNNER_PID_FILE",
                        tmp_path / "webrunner.pid")
    monkeypatch.setattr(module, "_chrome_slot", _FakeSlot())
    monkeypatch.setattr(module, "acquire_single_instance_lock",
                        lambda _path: _SpyLock())
    monkeypatch.setattr(module, "load_bot_config", lambda: {
        "webrunner_supervisor": {
            "respawn_backoff_min_sec": 300,
            "respawn_backoff_max_sec": 5,       # reversed
            "healthy_threshold_sec": 60,
            "rapid_fail_threshold_sec": 30,
            "rapid_fail_giveup_count": 3,
            "zero_progress_giveup_count": 3,
        }})

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("the config is broken but it still entered the supervise loop")

    monkeypatch.setattr(module, "_supervise", _must_not_run)
    monkeypatch.setattr(module, "stream_child", _must_not_run)

    assert module.main() == 1, "a broken backoff config should make the launcher wrap up with rc=1"


def test_a_pid_file_deleted_mid_read_counts_as_no_batch(monkeypatch, tmp_path):
    """Deleted between `exists()` and `read_text()`: that is equivalent to "the
    file does not exist", and you can start.

    This must be kept distinct from "cannot be read". Lumping them onto the
    conservative side means one race that happens to hit the wrap-up would make the
    launcher refuse to start, while the user's pid file is long gone — with no clue.
    """
    module = _load_launcher(WEBRUNNER_LAUNCHER)

    class _VanishingPidFile:
        def exists(self):
            return True

        def read_text(self, *_args, **_kwargs):
            raise FileNotFoundError("deleted by the wrap-up in this very instant")

    monkeypatch.setattr(module, "WEBRUNNER_PID_FILE", _VanishingPidFile())
    assert module._live_webrunner_pid() == (None, True), (
        "the file was deleted before it could be read, which is a **decidable** "
        "'no batch is running', not 'cannot decide'.")


if __name__ == "__main__":
    sys.exit(main())
