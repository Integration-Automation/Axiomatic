"""Helpers shared by process supervisor entry points.

`restart_backoff` and `child_exit_is_fatal` are pure functions.
`acquire_single_instance_lock` touches the filesystem (it has to — an OS-held
lock is the only staleness-free way to answer "is another instance already
running?").

The landing of child-process output (`stream_child` / `trim_log` / `say` as a
set) lives here too: both launchers must tee the child's stdout+stderr to the
console and the log file at once, and there should be only one implementation.
"""
from __future__ import annotations

import errno
import os
import subprocess  # nosec B404 — the supervisor's whole job is to spawn children
import sys
import threading
import time


# The child uses this rc to say "I didn't break — **another instance is already
# running**".
#
# Why a dedicated rc is needed: the supervisor's restart loop originally had only
# two give-up conditions, and neither catches this class of failure. Backoff only
# slows retries down, and rapid-fail giveup looks at "how long it ran" — but a
# child blocked by the lock exits **cleanly** within a second every time, so
# retrying a hundred times will never succeed, and every round still prints a
# "restarting in Ns" line. So we need an rc that lets the supervisor tell
# outright that "retrying is pointless".
#
# Why 3: 0 is a normal exit, 1 is an uncaught exception and general failure, 2 is
# CPython's own command-line error (`python nonexistent_file.py`). 3 is the first
# value not already taken. **Do not change it to 1**: that would blur it with a
# real crash and the supervisor could no longer tell them apart.
RC_ALREADY_RUNNING = 3

# The child uses this rc to say "**configuration is not filled in yet**" — the
# credentials file or `bot_config.json` is missing / still the template as
# shipped. Same class as `RC_ALREADY_RUNNING`: retrying a hundred times will
# never succeed, and every round reprints the same complaint. A brand-new clone
# always takes this path on its first launch, so it must be a legible sentence
# plus a clean exit, not a traceback plus endless respawning.
#
# **This one deliberately spans two rc contracts** (the bot's and the
# webrunner's), so it takes 5, which neither side has used yet: the bot uses
# 0–3, the webrunner uses 0–4. Both sides ask the same question ("this machine
# has no usable configuration yet") and have the same answer (stop and wait for a
# human), so there is no reason to give it two different numbers.
RC_SETUP_INCOMPLETE = 5

# ---- webrunner-specific rc contract (unrelated to the bot's rc above) --------
# Both supervisors (`start_webrunner.py` and the bot's `_watch_for_fallback`) use
# the values here to decide "should we respawn", kept in one place to stop the
# two from drifting.
#
# 3 = **zero output**: this round attempted generation but saved not a single
#     image (both the full-round zero-save backstop and the mid-round give-up
#     from "consecutive failures reaching the abort threshold" return this).
#     Respawning **once** is right — what broke is this session. But several
#     rounds in a row with zero output means respawning cannot fix it; stop.
#     Note that this value being 3 like `RC_ALREADY_RUNNING` is a coincidence and
#     they do not affect each other: that one is what the bot body says to the
#     bot launcher, this one is what the webrunner says to the webrunner
#     supervisor, and the two lines never cross. **Do not** use
#     `child_exit_is_fatal` to judge the webrunner's rc.
# 4 = **blocked**: the site pops up purchase / plan information, and retrying
#     will get nowhere. Respawning even once is too much.
RC_ZERO_PROGRESS = 3
RC_GENERATION_BLOCKED = 4


def webrunner_exit_needs_human(rc: int) -> bool:
    """After the webrunner exits: True = do not respawn, just stop and wait for
    a human.

    The criterion is the same as `child_exit_is_fatal` — "does retrying have any
    chance of succeeding" — but the subject differs (webrunner child vs bot
    child), so these are deliberately two functions.
    """
    return rc in (RC_GENERATION_BLOCKED, RC_SETUP_INCOMPLETE)


def child_exit_is_fatal(rc: int) -> bool:
    """After the child exits: True = the supervisor should not retry, just wrap
    up.

    Two cases: "another instance is already running" and "configuration is not
    filled in yet". The criterion is **whether retrying has any chance of
    succeeding**, not "how severe the error is": expired credentials or a wrong
    config value also will not succeed on retry, but their rc is indistinguishable
    from a real crash and can only be caught by rapid-fail giveup. The
    missing-file case is distinguishable, so it has its own rc.
    """
    return rc in (RC_ALREADY_RUNNING, RC_SETUP_INCOMPLETE)


def restart_backoff(
    current: float,
    *,
    minimum: float,
    maximum: float,
    healthy: bool,
) -> tuple[float, float]:
    """Return ``(wait_now, next_backoff)`` for a failed child process.

    A healthy run resets immediately. A short-lived failure waits for the
    current delay and only then increases the delay for the following retry.
    """
    if minimum <= 0 or maximum < minimum:
        raise ValueError("backoff requires 0 < minimum <= maximum")
    wait_now = minimum if healthy else min(max(current, minimum), maximum)
    next_backoff = minimum if healthy else min(wait_now * 2, maximum)
    return wait_now, next_backoff


class InstanceLock:
    """A held single-instance lock. The caller **should** keep it alive until
    the process ends.

    **This class deliberately has no `__del__`, and must not have one.** Measured
    (Windows 11 / CPython): `hasattr(InstanceLock, "__del__")` is False, and after
    dropping the last reference and calling `gc.collect()`, a second
    `acquire_single_instance_lock` still returns `None` — **the lock is still
    held**. Because the lock hangs off the open file description, nobody closes
    the fd so nobody releases the lock: when the object is collected the fd leaks,
    the lock stays held until the process ends, and mutual exclusion still holds.

    This is a deliberately chosen safe direction; **do not "finish off" the
    `__del__`**: leaking one extra fd hurts nobody (one per process, all reclaimed
    by the OS when the process ends), whereas silently releasing the lock lets a
    second instance start — two batch supervisors fighting over the same
    `.chrome_profile/`, each nuclear-sweeping the other's Chrome, while both logs
    look normal. `test_supervisor` guards this with two tests
    (`test_instance_lock_must_not_grow_a_del_method` pins the attribute,
    `test_dropping_the_reference_does_not_release_the_lock` pins the behaviour).

    This is **a different thing** from `acquire_single_instance_lock`'s "when it
    cannot decide, fall toward starting anyway"; do not conflate them: that one is
    about which way to fall **when the lock cannot be obtained** (toward allowing),
    this one is about what to do **after the lock has been obtained** when the
    reference disappears (keep holding it).

    `degraded=True` means "the locking mechanism itself is unavailable" (see the
    notes on `acquire_single_instance_lock`); in that case it is just an empty
    shell and guarantees no mutual exclusion.
    """

    __slots__ = ("_fd", "path", "degraded")

    def __init__(self, fd: int | None, path: str, degraded: bool = False):
        self._fd = fd
        self.path = path
        self.degraded = degraded

    def release(self) -> None:
        """Release the lock — **just a courtesy wrap-up**, not part of
        correctness.

        Whether the process exits normally, on an uncaught exception, or is cut
        down by `taskkill /F`, the OS closes the fd and releases the lock along
        with it, so failing to call this leaves no unbreakable stale lock behind.
        Deliberately made safe to call several times: the one in `finally` may
        have released already, and a `degraded` empty shell (`fd is None`) has no
        fd to close at all.
        """
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            os.close(fd)
        except OSError:
            pass


# The errno set for "this file is already locked by someone else". **Only these**
# mean "another instance is running"; any other errno means "the locking
# mechanism on this machine has a problem", which is an entirely different thing —
# blurring the two into one answer makes a concurrency-unrelated problem look
# exactly like "an instance is already running", and then the launcher refuses to
# start forever (see the notes on `acquire_single_instance_lock`).
#
# The two platforms give different answers, so **catch both** (measured locally,
# 2026-09-08):
#   * Windows `msvcrt.locking(fd, LK_NBLCK, 1)` gives **EACCES(13)** for an
#     already-locked region. The blocking `LK_LOCK` gives **EDEADLOCK(36)** when
#     its retries fail — we use the non-blocking version, so catching it is purely
#     insurance (if someone switches to the blocking version one day, at least it
#     will not be misread as "the lock broke").
#   * POSIX `flock(fd, LOCK_EX | LOCK_NB)` gives **EWOULDBLOCK/EAGAIN** for an
#     already-held file.
#
# `EWOULDBLOCK` on Linux is just `EAGAIN` (both 11), **but on Windows CPython it
# is not**: measured `errno.EAGAIN == 11` while `errno.EWOULDBLOCK == 10035`
# (Winsock's WSAEWOULDBLOCK). So you cannot write only one of them, nor assume
# the two are equal. `getattr` is used because these names are not guaranteed to
# exist on every platform; if one is missing, treat it as absent.
_LOCK_HELD_ERRNOS = frozenset(
    value for value in (
        getattr(errno, _name, None)
        for _name in ("EACCES", "EAGAIN", "EWOULDBLOCK", "EDEADLK", "EDEADLOCK")
    ) if value is not None
)


def acquire_single_instance_lock(path) -> InstanceLock | None:
    """Acquire an exclusive, process-lifetime lock on `path`, used to block "the
    same program being started twice". Acquired → return `InstanceLock`;
    **already held by another instance → return None**.

    **One lock file per program**: the launcher locks
    `.discord_bot_supervisor.lock`, the bot body locks `.discord_bot.lock`,
    deliberately kept separate. If they shared one file, the launcher would block
    the very bot it spawned — the one child the launcher must let through.

    An OS-level file lock is used rather than a pid file because a pid file has
    two incurable flaws: when the process is hard-killed the file lingers (every
    subsequent start refused forever), and the PID gets reclaimed and reassigned
    by the system (a lingering pid happens to match an unrelated process, again
    refusing every start forever). An OS lock releases automatically the instant
    the process disappears, even under `taskkill /F`, so there is no such thing as
    a leftover.

    **Which way to fall when it "cannot decide"**: fall toward "start anyway"
    (return a `degraded` shell), not toward "refuse to start". This is the
    opposite of CLAUDE.md's conservative direction for PID liveness probing, and
    it is deliberate — that rule guards "do not open a second Chrome stack", where
    the costs are symmetric; here the two mistakes have asymmetric costs: getting
    it wrong as "refuse" makes the bot **fail to start at all** over an unrelated
    filesystem problem with nobody noticing, while getting it wrong as "allow" at
    worst reverts to the state before this lock existed (duplicate instances).

    **Three outcomes, told apart by errno** (fixed 2026-09-08; before that there
    were only two, and any `OSError` counted as "already an instance", i.e. the
    policy above written backwards):

    | Outcome | Meaning | Return |
    |---|---|---|
    | Got the lock | Only I am running | `InstanceLock` (`degraded=False`) |
    | `_LOCK_HELD_ERRNOS` | Another instance is running | `None` |
    | Other errno / no `msvcrt`&`fcntl` / cannot open the file | **Cannot decide** | `InstanceLock(degraded=True)` |

    `degraded=True` is an honest report of "I provided no mutual-exclusion
    protection", and **the caller is responsible for saying so**: both launchers
    `say()` a line. Otherwise the allow is silent, and a silent degradation is the
    same thing as not having this lock at all.

    The child does not inherit this lock: since Python 3.4+ fds are
    non-inheritable by default (PEP 446), so the bot spawned under the launcher
    does not carry the lock away with it.
    """
    path = str(path)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as error:
        print(f"single-instance lock unavailable ({path}): {error!r}",
              file=sys.stderr)
        return InstanceLock(None, path, degraded=True)

    try:
        if os.name == "nt":
            import msvcrt
            # LK_NBLCK: non-blocking, locks 1 byte from the current position. If
            # already locked by someone else it raises OSError and does not hang.
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in _LOCK_HELD_ERRNOS:
            # Locked by another instance — this is the only path that returns None.
            try:
                os.close(fd)
            except OSError:
                pass
            return None
        # "cannot lock" is not the same as "someone holds it". Reaching here means
        # the lock call itself broke (EBADF, EINVAL, the ENOLCK a network drive
        # that does not support file locks gives, …), i.e. **cannot decide**, so
        # fall toward "start anyway" per the policy spelled out above.
        #
        # **This line deliberately changed a safety mechanism's failure
        # direction** (2026-09-08): before the change any `OSError` returned
        # `None`, so a filesystem problem with nothing to do with concurrency got
        # reported as "another instance is already running", the launcher then
        # **refused to start forever**, and the message even pointed at an instance
        # that did not exist — nobody could trace it. After the change an unknown
        # errno lets it through, at the cost of possible duplicate instances. The
        # latter is chosen because this function's policy already says to fall
        # toward "start anyway"; this just makes the code match it. And the allow
        # is **no longer silent**: `degraded` is printed by both launchers, and the
        # errno is left on stderr too.
        print(f"single-instance lock check failed (errno={error.errno}): "
              f"{error!r}; starting anyway, but without mutual exclusion this time",
              file=sys.stderr)
        return InstanceLock(fd, path, degraded=True)
    except ImportError as error:
        # The platform has no msvcrt/fcntl (extremely rare). Fall toward "start
        # anyway" as well.
        print(f"single-instance lock unsupported: {error!r}", file=sys.stderr)
        return InstanceLock(fd, path, degraded=True)

    return InstanceLock(fd, path)


def other_launcher_pids(script_name: str, *, self_pid: int | None = None,
                        procs=None, also_contains: str | None = None
                        ) -> list[int]:
    """The pids of **other** live processes running the `script_name` launcher.

    Used only as diagnostics for the "another instance is already running"
    message — **never part of a decision** (deciding is the single-instance lock's
    job), so when psutil is absent or unhappy, returning an empty list is fine and
    must not block startup.

    Lifted up from `start_discord_bot.py` on 2026-09-03 to be shared by both
    launchers. When lifted, the hardcoded `Path(__file__).name` became a
    parameter — placed here, `__file__` would resolve to `_supervisor.py` and
    never find any launcher.

    `procs` is injectable (an iterable of `(pid, name, cmdline, ppid)`), so this
    logic is testable without actually opening two launchers.

    `also_contains` additionally requires this argument to be on the command line.
    The bot's launcher is now **one process per platform** and shares one script
    filename, so matching on filename alone would let the discord one answer "yes"
    to "is the telegram one running". Passing `also_contains="telegram"` counts
    only that platform. **This is still only diagnostics**: a default platform
    started manually with `py -3 start_discord_bot.py` carries no `--platform`, so
    it is invisible here — real mutual exclusion is always the single-instance
    lock's job.
    """
    from axiomatic._process_control import (  # deferred import: avoids a startup dependency cycle
        cmdline_runs_script,
        collapse_interpreter_stub_pairs,
        looks_like_python_process,
    )

    me = os.getpid() if self_pid is None else self_pid
    raw: list[tuple[int, int, str]] = []
    if procs is None:
        try:
            import psutil  # type: ignore
        except ImportError:
            return []

        def _iter():
            # Filter first with the cheap `name`, then read `cmdline()` / `ppid()`
            # only for Python processes. On Windows psutil rebuilds the whole
            # machine's mapping table every time you fetch ppid, so writing
            # `attrs=[..., "ppid"]` is O(N²) (measured 3.7 seconds, and this is a
            # path that runs on every startup).
            for proc in psutil.process_iter(attrs=["pid", "name"]):
                try:
                    yield (proc.info.get("pid"), proc.info.get("name"),
                           proc.cmdline(), proc.ppid() or 0)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        procs = _iter()

    try:
        for pid, name, cmdline, ppid in procs:
            if not looks_like_python_process(name):
                continue
            # The condition is tightened to "the argument is this script's path":
            # substring matching would count any command line that **mentions**
            # the filename (a shell, `python -c`) as "another instance".
            try:
                hit = cmdline_runs_script(cmdline, (script_name,))
            except Exception:  # pylint: disable=broad-except
                continue
            if hit and also_contains is not None:
                # Match a whole argument verbatim, not a substring: `--platform
                # telegram` is split into two argv elements, whereas a substring
                # match would let a name like `telegram2` match by mistake.
                hit = any(str(arg) == also_contains for arg in (cmdline or ()))
            if hit:
                # **Keep** our own entry and let collapse exclude it — it needs to
                # see our own ppid to drop the "our own interpreter stub" half.
                raw.append((pid, ppid, "launcher"))
    except Exception:  # pylint: disable=broad-except
        pass
    # When the launcher runs via `.venv\Scripts\python.exe`, the scan hits two
    # entries, "stub + real" (identical cmdline, parent/child relationship). If
    # not collapsed, one existing instance gets reported as two pids and the
    # reader thinks they really opened two copies.
    return [pid for pid, _script in
            collapse_interpreter_stub_pairs(raw, exclude_pid=me)]


# ---- Landing child-process output (shared by both launchers) ----------------
#
# On 2026-08-23 this was already hit and fixed on the webrunner side: the
# launcher was `subprocess.run(cmd)`, the child inherited the console directly,
# and **once the window was closed that line was gone forever**. That fix was
# applied only to `start_webrunner.py`; `start_discord_bot.py` kept the same
# pattern untouched until 2026-08-30.
#
# The bot side is actually worse, because the whole design of Secrecy Layer 1
# rests on "send a generic message to Discord, write full detail to the log" —
# the bot even replies "please check the log". With no landed file, that sentence
# points at something that does not exist, and every `print(..., file=sys.stderr)`
# diagnostic (including background-task crashes, raw exception text, and the
# supervisor's give-up reason) lives only in some console nobody is watching.
#
# So the implementation lives here in one copy, used by both launchers. **Do
# not** write it again separately in each launcher.

_LOG_PUMP_JOIN_SEC = 5.0


def trim_log(path, *, max_bytes: int, keep_bytes: int) -> None:
    """When `path` exceeds `max_bytes`, keep only the last `keep_bytes` (cut on a
    whole-line boundary).

    Best-effort: any I/O failure just means no trim; never let the supervisor die
    over a log file. The tail is kept rather than the whole thing cleared — the
    seam that blows up is exactly "crash → respawn", and clearing it throws away
    the very thing you need to investigate.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    try:
        with path.open("rb") as handle:
            handle.seek(size - keep_bytes)
            handle.readline()          # drop the half-line where the seek landed
            tail = handle.read()
        path.write_bytes(tail)
    except OSError as error:
        print(f"trim_log({path.name}) failed: {error!r}", file=sys.stderr)


def echo_line(line: str) -> None:
    """Write one line from the child back to the launcher's own console. **Under
    no circumstances may it throw an exception outward.**

    The child's output is forced to UTF-8 (see `stream_child`), but the
    **launcher**'s stdout is not necessarily a console — when redirected to a file
    it uses the system locale encoding, and hits `UnicodeEncodeError` on a
    character it cannot encode. The supervisor must not die over one log line, so
    it falls back to a replace-on-error write.

    The two `try` blocks here are **separate**, not two `except` clauses of one
    `try` (fixed 2026-09-07). Both things went wrong on their own:

    1. An exception thrown from inside an `except` block is **not** caught by
       another `except` of the same `try`. The fallback used to be a direct
       stdout write inside `except UnicodeEncodeError:` with an
       `except OSError: pass` hung below it — the latter covers the former not at
       all. Measured: the first write threw `UnicodeEncodeError`, the fallback
       threw `OSError(28)`, and that `OSError` shot straight out of this function.
    2. **Writing to a closed stream throws `ValueError`, not `OSError`.** This
       function runs on the pump thread, and `pump_stream`'s
       `except (OSError, ValueError)` wraps the whole loop — so one broken console
       equals **the entire pump stopping**, and the child then fills the pipe and
       hangs forever. Measured: 3 lines, only 1 pumped. That is worse than having
       no log at all: the supervisor is still alive, the batch is stuck, and it
       says nothing.
    """
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
        return
    except UnicodeEncodeError:
        pass                    # fall through to the replace-on-error write
    except (OSError, ValueError):
        return
    # The fallback path wraps itself again. `getattr` for `encoding`: this object
    # is not one we control (it could be any redirection wrapper), and a missing
    # attribute must not let the supervisor die over one log line either.
    # `LookupError` / `UnicodeError` cover "the stream lies about its encoding".
    try:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.write(line.encode(enc, "replace").decode(enc, "replace"))
        sys.stdout.flush()
    except (OSError, ValueError, UnicodeError, LookupError):
        pass


def log_write(log, line: str) -> None:
    """Write one line to the log file, prefixed with a timestamp (the console
    copy stays as-is, no prefix).

    The timestamp is for reconciliation after the fact — `events.ndjson`'s `ts`
    must line up with the lines here.
    """
    if log is None:
        return
    try:
        log.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {line}")
    except (OSError, ValueError):
        pass


def say(log, message: str, *, err: bool = False) -> None:
    """The supervisor's own messages: one copy to the console, one to the log
    file.

    Give-up reasons, rcs, and backoff seconds are exactly what after-the-fact
    diagnosis needs to see, and printing only to the console keeps nothing.
    """
    print(message, file=sys.stderr if err else sys.stdout)
    log_write(log, message.strip() + "\n")


def pump_stream(stream, log) -> None:
    """Send the child's output line by line to the console + log file, until EOF.

    **This pump loop runs on its own thread and must never stop**: as soon as
    `stdout=PIPE` is given but nobody reads, once the OS pipe buffer (about 64 KB
    on Windows) fills, the child's next print hangs forever — worse than having no
    log at all. The point of a separate thread is to keep pumping even the wrap-up
    output after Ctrl+C, so that waiting for the child to finish does not instead
    wedge it. Never raises.
    """
    if stream is None:
        return
    try:
        for line in stream:
            echo_line(line)
            log_write(log, line)
    except (OSError, ValueError):
        pass


def reap_child(proc, log, *, grace_sec: float, kill_sec: float) -> int:
    """Reap the child cleanly after Ctrl+C: first wait for it to finish on its
    own, terminate on timeout, then kill on a further timeout.

    Why you cannot just re-raise KeyboardInterrupt and walk away: that would leave
    an orphan child (and, on the webrunner side, a whole Chrome tree).
    `subprocess.run` on KeyboardInterrupt does `process.kill()`, which on Windows
    is TerminateProcess, so the child's `finally` does not run at all. Here it is
    courtesy first, force after.
    """
    try:
        return proc.wait(timeout=grace_sec)
    except subprocess.TimeoutExpired:
        say(log, "supervisor: child did not exit in time; terminating", err=True)
        proc.terminate()
    try:
        return proc.wait(timeout=kill_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait()


def stream_child(cmd: list[str], log, *, cwd: str, on_spawn=None,
                 pump_name: str = "log-pump",
                 grace_sec: float = 30.0, kill_sec: float = 10.0) -> int:
    """Run the child once, tee its stdout+stderr to the console and the log file
    at once, and return the rc.

    The child is forced to `PYTHONIOENCODING=utf-8`: a pipe is not a console, so
    CPython falls back to the system locale encoding (cp950 here), and Chinese
    output can then blow up the **child** itself. The decode end also uses
    `errors="replace"`, so no odd byte interrupts supervision. The caller's `-u`
    still has to stay, or the child's output would pile up in its own buffer and
    the log would come out in bursts.

    **"Forced" = overwrite, not `setdefault`** (fixed 2026-09-12). This line used
    to be `env.setdefault(...)`, while the note above has said "forced" from day
    one — the difference is who wins when the caller's environment **already
    carries a value**. Overwrite is chosen because the decode end of the `Popen`
    below is a **hardcoded** `encoding="utf-8"`: any mismatch between the two ends
    is silent data corruption, `errors="replace"` guarantees no exception, and so
    the rc is normal, there is no red text, and only the Traditional-Chinese
    progress lines, resume-mismatch reasons, and give-up reasons in the log turn
    into whole runs of U+FFFD. And the launcher's most common start paths (desktop
    shortcut, autostart, scheduled task, someone else's shell) are exactly the
    ones that carry "a value in the environment we never set". The other 16 places
    in this project that specify a child's encoding all overwrite
    (`{**os.environ, "PYTHONIOENCODING": "utf-8"}`); this was the one exception,
    and its direction happened to be the silently-broken side.

    It also deliberately does **not** take the middle path of "only overwrite when
    it is not a UTF-8 variant" (letting through `utf-8:surrogateescape` and the
    like): that would need a stretch of codec normalisation (and `codecs.lookup`
    itself throws `LookupError`), buying only the preservation of the caller's
    error handler — which has no effect on our end, because we are already
    `errors="replace"`. What the supervisor's log looks like should not depend on
    who lit it up, or from which shell.

    Behavioural tests cover both halves (`test_supervisor.py`): the variable
    absent from the environment (`test_the_child_gets_utf8_io_encoding`), and a
    wrong value present in the environment
    (`test_a_wrong_pythonioencoding_in_the_parent_is_overridden`). **Only the
    latter tells overwrite apart from `setdefault`.** `test_text_encoding`'s
    static scan structurally cannot see this site — `cmd` is a variable the caller
    supplies, and the scanner cannot recognise this as a Python child — so these
    two named tests are its entire coverage.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(  # nosec B603 — cmd is assembled by the caller, no shell
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        if on_spawn is not None:
            on_spawn(proc)
        pump = threading.Thread(target=pump_stream, args=(proc.stdout, log),
                                name=pump_name, daemon=True)
        pump.start()
    except Exception:  # pylint: disable=broad-except
        # The child **is already up**, so this path cannot just re-raise (fixed
        # 2026-09-07). `on_spawn` does real I/O — `start_webrunner._on_spawn`
        # writes `webrunner.pid` — and a full disk or wrong permissions throw
        # OSError. Re-raising would leave an orphan whose stdout is a PIPE nobody
        # pumps and nobody waits on: its next print wedges in the filled pipe, and
        # it is holding a whole Chrome tree. The supervisor itself dying while the
        # batch sits there stuck is exactly the hardest kind of ending to
        # diagnose. Reap first, then re-raise the exception.
        #
        # `grace_sec=0`: the child here did **not** receive Ctrl+C and has no
        # reason to finish on its own, so waiting out a grace period is wasted
        # time — terminate straight away, and any leftover Chrome gets swept by
        # `_kill_orphan_chrome()` on the webrunner's next start.
        #
        # **Do not** change these two handlers to `except BaseException`: project
        # rules forbid it (`test_exception_handlers.test_nothing_swallows_cancellation`,
        # because that would swallow `CancelledError` / `KeyboardInterrupt` too),
        # and it is not needed here either — what actually leaves an orphan is
        # `on_spawn` throwing `OSError` (the pid file cannot be written), which is
        # an `Exception`. A Ctrl+C landing in this tiny window is already covered
        # by the **console group**: the child and the launcher are in the same
        # group, so the child received the same Ctrl+C itself.
        try:
            say(log, "supervisor: spawn hook failed; terminating the child "
                     "that was already started", err=True)
            reap_child(proc, log, grace_sec=0, kill_sec=kill_sec)
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass               # a failed reap must not mask the original exception
        raise
    try:
        return proc.wait()
    except KeyboardInterrupt:
        # Same console group, so the child received Ctrl+C too. The pump thread is
        # still alive, so even a burst of wrap-up output will not wedge it in a
        # filled pipe.
        reap_child(proc, log, grace_sec=grace_sec, kill_sec=kill_sec)
        raise
    finally:
        pump.join(timeout=_LOG_PUMP_JOIN_SEC)
        # If the pump is still stuck in read, **do not** close (condition added
        # 2026-09-07). Measured: `BufferedReader.close()` throws no exception and
        # does not wrest the stream away from the pump — it contends for the same
        # lock, and so **blocks until that read returns** (measured 19.05 seconds,
        # exactly how long the child stayed alive).
        #
        # The normal path never reaches this: `proc.wait()` returning = the child
        # is dead = pipe EOF = the pump ends immediately. What does reach it is the
        # "reap failed, child still alive" path (`reap_child`'s `terminate()` /
        # `kill()` throwing their own exception) — then this line would wedge the
        # supervisor **permanently** at the last step of wrapping up, while it
        # still holds the single-instance lock, so nobody can restart and nothing
        # shows on screen. The pump is a daemon thread, so the OS reclaims the fd
        # when the process ends.
        if proc.stdout is not None and not pump.is_alive():
            try:
                proc.stdout.close()
            except OSError:
                pass
