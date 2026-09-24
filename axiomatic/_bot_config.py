"""Shared loader for `bot_config.json`.

Loaded once by `discord_bot.py` at import time (before any handlers run).
Changes require `!restart` to take effect — most values wire into things
set up before the message dispatcher starts (channel_id, presence target,
probe interval task), so live reload would require restarting Discord
client tasks anyway.

Missing / malformed file → return defaults, never raise. Wrong-type
values fall back to the per-key default with a stderr warning, and a key this
module does not recognise (a typo) gets its own one-off warning — the two look
identical from the user's side (the setting silently does nothing).

**The "with a stderr warning" clause was a lie before 2026-09-09**: the whole
module had only two `print`s, both for the **entire file** being unreadable /
unparseable, and not a single per-key warning. The symptom was that a user hand-
edited `bot_config.json`, mistyped one value's type → silent fallback to the
default → and because a change to this file **only takes effect after a
restart**, they restarted, assumed the setting had applied, and were actually
running the default with no clue at all (the only way to notice was to reconcile
against `_DEFAULT_BOT_CONFIG` by hand). Now every flat key goes through
`_take()` / `_COERCERS` and each nested section through its own small table, so
"added a setting but forgot to add its warning" is no longer "remember to do it"
but impossible.

**The "Loaded once ... at import time" line above holds only for
`discord_bot.BOT_CONFIG`, not for this function itself** — `_external_apis.
_user_agent()` calls `load_bot_config()` once on every outbound HTTP request
(reading `api_contact`), `dorossi_backend` keeps its own copy, and `/config
reload` reloads it again. So repeated complaints must be collapsed by
`_warn_once`, otherwise one uncorrected typo would wash out the whole log file
with the same line (`discord_bot.log` has already had this happen once for
another reason: 96% of the lines were the same sentence).
"""
from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

# The ceiling that `float()` can represent. Comparing against a huge int does
# not overflow, so we use it as the "can this number become a float?" gate
# (see `_is_finite_number`).
_FLOAT_MAX = sys.float_info.max

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOT_CONFIG_FILE = _PROJECT_ROOT / "bot_config.json"

_DEFAULT_SUPERVISOR: dict = {
    "fallback_window_sec": 300,
    "respawn_backoff_min_sec": 5.0,
    "respawn_backoff_max_sec": 300.0,
    "healthy_threshold_sec": 60.0,
    # Rapid-fail giveup: if webrunner exits in < rapid_fail_threshold_sec
    # `rapid_fail_giveup_count` times in a row, supervisor posts an alert
    # and stops respawning. User must `!run` to retry. Prevents the
    # "Chrome can't init → infinite respawn loop redoing first-time setup
    # → channel spam" scenario.
    "rapid_fail_threshold_sec": 30.0,
    "rapid_fail_giveup_count": 5,
    # Zero-progress giveup: rapid-fail only catches runs that die FAST. When
    # generation itself is blocked (the site pops a purchase / account dialog),
    # every run burns the whole `consecutive_fail_abort` budget first — minutes,
    # not seconds — so it never looks rapid and the supervisor respawns forever.
    # This counts consecutive runs that exited rc=3 (attempted generation, saved
    # nothing) and stops after `zero_progress_giveup_count` of them. 2 = respawn
    # once, then stop; 1 = never respawn a zero-output run.
    "zero_progress_giveup_count": 2,
    # Retry valve for the `/gen image` one-shot. **This is a different thing from
    # the ones above**: those are parameters for `_watch_for_fallback` (the batch
    # supervisor), while these three govern the path where
    # `_reap_oneshot_webrunner` reaps the child and re-drives the queue. The one-
    # shot deliberately has no batch supervisor attached (its rc=0 idle exit is a
    # normal ending and must not trigger a je->selenium switch / backoff
    # respawn), so before 2026-09-11 that path had **no ceiling at all**: the
    # background process would start, die, get reaped, restart, and the same
    # request would never be served, so the re-drive condition was always met (in
    # a synthetic environment, measured at about 9100 loops per second, each one
    # running a nuclear sweep that killed every chrome on the machine).
    # giveup_count = how many consecutive deaths of the same request before
    # giving up (1 = no retry at all).
    "oneshot_retry_giveup_count": 3,
    "oneshot_retry_backoff_min_sec": 5.0,
    # Deliberately smaller than respawn_backoff_max_sec (300): that one protects
    # a multi-hour unattended batch, while this one has a person watching the
    # placeholder message underneath it.
    "oneshot_retry_backoff_max_sec": 120.0,
}

_DEFAULT_GUI_CONTROL: dict = {
    # `!launch <name>` only accepts programs that appear in this list. Case-
    # insensitive; matching strategy: (1) full string (with/without .exe)
    # (2) basename match. Empty list → `!launch` is fully disabled (replies
    # "no programs whitelisted"). Put the .exe directly here, or an absolute
    # path (a path is launched as-is). Even a CHANNEL_ID-restricted `!` goes
    # through this, because once the token leaks it is too risky to let a
    # channel attacker start arbitrary programs.
    "launch_whitelist": [],
    # Alias → launch target. Target can be an exe path, a `steam://`
    # rungameid URI, or anything `os.startfile()` can handle. `!launch <key>`
    # first matches this dict (case-insensitive), then falls through to the
    # whitelist. Lets the user type `!launch mygame` without memorising
    # `steam://rungameid/000000`.
    "launch_aliases": {},
}


_DEFAULT_USER_ROLES: dict = {
    # Optional permission split for `!` commands. Empty lists preserve the
    # historical channel-gated behavior. OWNER_USER_ID is always admin in
    # discord_bot.py even when these are empty.
    "admin_user_ids": [],
    "operator_user_ids": [],
    "viewer_user_ids": [],
}


_DEFAULT_DAILY_HEALTH_REPORT: dict = {
    "enabled": False,
    "channel_id": 0,
    # Local bot process time, HH:MM, checked once per minute.
    "time": "09:00",
}


_DEFAULT_DASHBOARD: dict = {
    "host": "127.0.0.1",
    "port": 8765,
}


# Daily backend model-catalogue check (2026-09-23). Hung off the existing
# once-a-minute health loop, throttling itself with `interval_hours`; the last-
# check time is stored in the model-catalogue file, so a restart does not re-run
# it. `announce_channel_id` 0 = reuse the health report's channel (which itself
# 0 = `channel_id`).
_DEFAULT_DOROSSI_MODEL_CHECK: dict = {
    "enabled": True,
    "interval_hours": 24.0,
    "announce_channel_id": 0,
}

# Other chat platforms. **One section per platform, all disabled by default.**
#
# Besides `enabled`, the three keys are "on this platform" identifiers, so they
# are **strings** rather than integers: id shapes differ between platforms (some
# numeric, some alphanumeric), and unifying on string comparison keeps a valid
# id from silently becoming "not the owner" when a type conversion fails.
#
#   owner_user_ids  ── the owners on this platform. `OWNER_USER_ID` is an id
#                      **on one particular platform** and does not hold on
#                      another, so each platform lists its own. **Empty list =
#                      nobody on this platform can pass the host-control gate**
#                      (fail-closed).
#   allowed_chat_ids ─ this platform's "command channel". A conversation listed
#                      here is the equivalent of the existing `channel_id`: a
#                      non-owner may only issue commands in these conversations,
#                      everything else is silently ignored. Empty list = owner-
#                      only (the most conservative default).
#   poll_timeout_sec ─ how long a single long-poll waits.
_DEFAULT_PLATFORM_TELEGRAM: dict = {
    "enabled": False,
    "owner_user_ids": [],
    "allowed_chat_ids": [],
    "poll_timeout_sec": 30.0,
}

# The default platform's own section has only a master switch. Its identity
# settings live at the **top level** (`channel_id`, `owner_user_id`,
# `discord_bot_token.md`), so they are not duplicated here; and it is **enabled
# by default** — defaulting it off would make a fresh-clone bot do nothing, with
# symptoms identical to "the setting did not take effect". Turning it off uses
# the same form as turning off any other platform; "every platform can be
# individually disabled" includes it.
_DEFAULT_PLATFORM_DISCORD: dict = {
    "enabled": True,
}

_DEFAULT_PLATFORMS: dict = {
    "discord": _DEFAULT_PLATFORM_DISCORD,
    "telegram": _DEFAULT_PLATFORM_TELEGRAM,
}


_DEFAULT_BOT_CONFIG: dict = {
    # The channel ID that channel-restricted commands apply to. **No usable
    # default**: 0 = not yet configured, and the bot blocks at startup and tells
    # the user to copy `bot_config.example.json`.
    "channel_id": 0,
    # The owner's Discord user ID. The host-control command groups
    # (`_OWNER_ONLY_GROUPS`) and Dorossi accept only this one ID. 0 = not
    # configured; a Discord user ID can never be 0, so this default is fail-
    # closed — those commands are all refused rather than open to anyone.
    "owner_user_id": 0,
    # Channel IDs allowed to display full host paths (owner ruling 2026-08-25).
    # The hard rule "no host paths on Discord" originally opened one gap for 1:1
    # DMs; this list extends the same gap to named channels, letting the owner
    # see the full working directory in their own channels rather than only the
    # last segment. Empty list (default) = keep the DM-only status quo; every
    # other channel gets only `_dorossi_dir_leaf`'s trailing segment. This is a
    # **surface** gate, not an identity gate: for a channel in the list, other
    # people in that channel also see those paths, so only put channels the owner
    # controls themselves in it.
    "path_reveal_channel_ids": [],
    # Contact info for external APIs, wired into the User-Agent
    # (`_external_apis._user_agent`). "Say who you are" and "be reachable" are
    # two different things, and some sites only accept the latter: measured
    # 2026-08-30, Wikimedia's REST API still returns 403 for a purely descriptive
    # UA, with the message "Please respect our robot policy … Contact
    # bot-traffic@wikimedia.org"; adding a URL or email turns it into a 200.
    # Empty string (default) = no contact info attached, and those sites keep
    # returning 403. What to put here is the owner's decision, not something the
    # code should presume — this string is sent to third-party sites.
    "api_contact": "",
    # Which user's Discord presence to mirror; empty string (default) = no
    # mirroring.
    "target_presence_username": "",
    "default_help_lang": "zh-tw",
    "presence_probe_interval_sec": 8.0,
    "event_poll_seconds": 5.0,
    # Discord user ID to @-mention on critical webrunner alerts (0 = no ping).
    "alert_user_id": 0,
    # Minimum free disk (GB) on the output drive; gates !run + mid-run
    # monitoring. 0 = disabled.
    "min_free_disk_gb": 5.0,
    # During batch supervision (including the stretch waiting for the network to
    # return after an outage) and while a Dorossi job runs, the bot holds a power
    # request (`_power_request`, `PowerRequestExecutionRequired`) so this bot
    # process is not suspended under Modern Standby — otherwise a batch that
    # crashes during standby waits until the host wakes for anything to respawn
    # it. It protects the bot process itself, not "the system does not enter
    # standby", and does not cover child processes; on DC power the system
    # revokes it 5 minutes after the sleep timeout. Enabled by default; false =
    # back to the old behaviour where the bot holds nothing.
    "keep_bot_awake": True,
    # Backend for the `@bot Dorossi` command. "claude_code" shells out to the
    # local Claude Code CLI (`claude -p`) so it rides the host login's plan
    # (e.g. a Pro/Max subscription) instead of metered API billing; "api"
    # calls the Anthropic API directly via the SDK.
    "dorossi_backend": "claude_code",
    # Tool mode for `@bot Dorossi`'s claude_code backend. "off" (default) = pure
    # chat, all tools disabled (passes an empty tool whitelist `--tools ""`, plus
    # an enumerated --disallowedTools as a second layer; no bypassPermissions),
    # the safest; "full" = full agent (--permission-mode bypassPermissions + all
    # tools enabled, can run a shell / read and write files on the host, requires
    # the owner to authorise it themselves). Only takes effect when backend is
    # claude_code; the api backend is unaffected.
    "dorossi_cc_tools": "off",
    # The "hard wall-clock watchdog ceiling" (seconds) for Dorossi's claude_code
    # backend, split by tool mode:
    #   *_off  ── pure chat (dorossi_cc_tools="off"): the answer is bounded, no
    #             tools, low odds of a hang, so it keeps the tighter default of
    #             900s (15 minutes).
    #   *_full ── full agent (dorossi_cc_tools="full"): the owner runs long
    #             agentic tasks (multi-subagent orchestration, waiting on
    #             background subagents, running the whole test suite). On
    #             2026-09-19 the owner reported "the wait is too short, tasks keep
    #             getting killed" (on 09-18 22:50 a round was cut off by the hard
    #             ceiling at 3600s while a tool was still running), so the default
    #             was raised from 3600s to 10800s (3 hours). It stays bounded,
    #             though — the hard ceiling cannot be removed, because under full
    #             mode's bypassPermissions a hung tool would keep the idle tier
    #             from ever firing, so a wall-clock ceiling is the backstop; and
    #             because of the queue lock, a round runs at most as long as it
    #             holds the lock, so this ceiling is also the guarantee that the
    #             queue keeps advancing. The self-running mode borrows it too:
    #             while background work is still live, output silence is not cut,
    #             but only up to this many seconds.
    # Both values clamp to no less than DOROSSI_CC_HARD_LIMIT_FLOOR_SEC to keep
    # the protection from being disabled by a 0 / negative setting.
    "dorossi_cc_hard_limit_off_sec": 900.0,
    "dorossi_cc_hard_limit_full_sec": 10800.0,
    # The "idle ceiling" (seconds) for a single-turn Dorossi Q&A: only cut as
    # stuck after this long with no output at all **and** no tool running and no
    # background work reported by the CLI. While a tool or background work is
    # running it does not fire, and the hard ceiling above closes it out. Before
    # 2026-09-19 this was hardcoded at 300s and not configurable; after the owner
    # reported the wait was too short it became configurable, default 600s.
    # Clamps to no less than DOROSSI_CC_HARD_LIMIT_FLOOR_SEC — too small would cut
    # every answer that needs a moment's thought first. **Setting it larger than
    # the hard ceiling is also safe**: each wait is min(idle, remaining until the
    # hard ceiling), the hard ceiling always arrives first, and the idle tier
    # simply loses its chance to fire. The self-running mode does not use this
    # value (there it is the silence ceiling below).
    "dorossi_cc_idle_limit_sec": 600.0,
    # The per-round "output-silence backstop" (seconds) for Dorossi's "self-
    # running mode". Self-running mode sets no round-count ceiling and does not
    # apply the hard wall-clock ceiling above — it uses this silence ceiling as
    # the backstop instead: a round with no new output at all for more than this
    # many seconds is always terminated (handed to the loop's silence retry; even
    # if a foreground tool is still executing — the key difference from the
    # ordinary idle tier, so a hung tool cannot wedge an unattended loop
    # forever). **The one exception is when the CLI reports background work**
    # (background subagent, background shell, watch job): that silence is spent
    # waiting on it, so it is not cut, but only up to
    # dorossi_cc_hard_limit_full_sec. The default was relaxed on 2026-09-19 from
    # 600s to 1800s (a foreground tool waiting on a background subagent blocks for
    # at most 600s, which collided exactly with the old value); it likewise
    # clamps to no less than DOROSSI_CC_HARD_LIMIT_FLOOR_SEC to keep a 0 /
    # negative setting from disabling this layer of protection.
    "dorossi_loop_silence_limit_sec": 1800.0,
    # Wait strategy when Dorossi's self-running mode hits the "plan usage limit".
    # The backend's usage resets on a rolling 5-hour window, and the old
    # behaviour (stop the whole loop on hit, leave loop_pending for a manual
    # `/dorossi session continue`) meant an unattended long task needed a human
    # several times a day, i.e. could not really finish. It now sleeps until the
    # allowance returns and resumes on its own. Three keys:
    #   fallback ── when no machine-readable reset time is available (only a
    #               zone-less clock time like `resets 3:45pm`, or none at all),
    #               the seconds to wait the first time; each consecutive hit
    #               after that doubles it.
    #   max      ── the ceiling on a **single** wait. Default 6 hours, slightly
    #               larger than the 5-hour rolling window, so one wait is enough
    #               to cover a full window; if the backend reports a further-out
    #               time (a weekly limit, say) it sleeps at most this long and
    #               then probes again — probing is cheap, oversleeping is
    #               irreversible waste.
    #   max_consecutive ── give up the whole loop after this many consecutive
    #               waits with not a single successful round. **0 = no limit
    #               (default)**, matching the owner's ruling of "no round/cost-
    #               style limit"; waiting itself costs nothing, so the default is
    #               to let it keep waiting.
    # The floor (60s) and buffer (60s) are code constants, not configurable:
    # they are the spin protection for a false "usage limit" detection, see
    # dorossi_backend.DOROSSI_USAGE_WAIT_MIN_SEC.
    "dorossi_usage_wait_fallback_sec": 900.0,
    "dorossi_usage_wait_max_sec": 21600.0,
    "dorossi_usage_wait_max_consecutive": 0,
    # Stop after this many consecutive "transient server failures" (529
    # Overloaded / 5xx) with not a single successful round. Default 20: with
    # exponential backoff (30s start, capped at 15 min) that is roughly enough to
    # ride out a three-hour outage, after which still not stopping no longer
    # looks like "just wait a moment". 0 = no limit.
    "dorossi_transient_max_consecutive": 20,
    # Retry ceiling for unexpected errors. An unattended long task should not be
    # ended by one incidental failure (backend process killed, network jitter, an
    # unforeseen exception); but retrying forever just turns "broken" into
    # "silently broken forever". 3 tries with 20s->40s->80s backoff is enough to
    # absorb the incidental without dragging on. 0 = no retry.
    "dorossi_error_retry_max": 3,
    # Retry ceiling for output silence (the backend hanging on this round). A
    # hang is usually the process, and respawning once often clears it;
    # consecutive hangs are what signal it is not incidental. 0 = no retry (keeps
    # the old behaviour).
    "dorossi_silence_retry_max": 2,
    # Self-running loop "auto-resume across a bot restart". Waiting for the
    # allowance to return solves "the backend is blocking"; this set solves "the
    # process is gone" — a restart or a host crash makes the loop and its wait
    # evaporate together, and the old behaviour left only a loop_pending for a
    # manual resume. On startup the bot re-adopts any marker still "alive".
    #   max_age_sec ── how recent the marker's heartbeat must be to auto-resume
    #                  (seconds). **0 = auto-resume disabled**, back to a purely
    #                  manual `/dorossi session continue`. Default 86400 (24
    #                  hours): covers "one usage wait (up to 6 hours) + a stretch
    #                  of host downtime" without suddenly starting a task the
    #                  owner has long forgotten a week later.
    #   max_tries   ── stop auto-resuming after this many consecutive resumes
    #                  where not a single round completed (the circuit breaker for
    #                  a crash loop). Any completed round resets it to zero, so a
    #                  healthy long task never accumulates it. 0 = no limit.
    # It auto-resumes only when "the previous process was killed": a voluntary
    # loop ending (abort / silence backstop / exception / giving up) writes the
    # marker non-live in `finally`, and `finally` does not run when the process is
    # killed.
    "dorossi_loop_autoresume_max_age_sec": 86400.0,
    "dorossi_loop_autoresume_max_tries": 5,
    # The dollar spend ceiling for "each `claude -p` invocation (i.e. each self-
    # running round, and each call of a single-turn Q&A)" of Dorossi's
    # claude_code backend, passed via the CLI's --max-budget-usd. This is a "per-
    # call" spend gate, not a round-count limit. 0 = disabled (the flag is not
    # passed). **Disabled by default (owner ruling)**: the owner explicitly ruled
    # that "there should be no limit other than the backend's own usage limit"
    # (they actually hit error_max_budget_usd, a single round blocked by the old
    # 5.0 default). This key is kept for anyone who later wants to set their own
    # limit by hand; do not add a non-zero default back.
    "dorossi_max_budget_usd": 0.0,
    # The "periodic compaction" trigger threshold for Dorossi's "self-running
    # mode" — a real fix: self-running has no round ceiling, and resume re-sends
    # the whole growing conversation every round, so tokens are ~O(N²). Every so
    # many rounds / or when the spend accumulated "since the last compaction"
    # crosses the threshold, it inserts an in-place `/compact` round (same session
    # id, preserving the task / todo context, squashing the old history), so the
    # prefix that resume re-sends afterwards shrinks a lot. It compacts context,
    # not round count (compatible with the "self-running has no round ceiling"
    # ruling). Either threshold triggers it; each 0 = that condition disabled.
    #   *_rounds ── compact every this many "work rounds". Compaction is lossy (it
    #               summarises away detail) and misaligns the cache once, so the
    #               default is the more conservative (less frequent) 10.
    #   *_cost_usd ─ compact once the dollar spend accumulated "since the last
    #               compaction" reaches this value (covering the "a few rounds
    #               burn a lot" case that a round count misses).
    "dorossi_loop_compact_every_rounds": 10,
    "dorossi_loop_compact_cost_usd": 10.0,
    # Dorossi's "auto-compact when the context gets too big" threshold (tokens),
    # shared by single-turn Q&A and the self-running loop. A round's context size
    # sent to the backend ≈ fresh input + cache_read + cache_creation; crossing
    # this threshold inserts an in-place `/compact` for that session (same session
    # id, preserving the task / todo context, squashing the old history), so the
    # prefix that resume re-sends afterwards shrinks a lot. It compacts context,
    # not round count (compatible with the "self-running has no round ceiling"
    # ruling). This is the owner-mandated sole means of reducing tokens — it does
    # not touch the effort / model / tool settings. Default 300000; 0 = this
    # condition disabled. The self-running loop also has round-count / spend
    # triggers, and any of the three triggering compacts.
    "dorossi_compact_context_tokens": 300000,
    # Dorossi single-turn session hygiene (conservative): if an active session
    # has gone unused for more than this many days, the next round auto-clears its
    # context (starts from a fresh conversation), so a long-lived single-turn
    # session does not grow without bound. Silent amnesia is a poor experience,
    # and `@bot session` already allows a manual reset, so the threshold takes a
    # conservative "clearly too old" value; 0 = disabled.
    "dorossi_session_max_age_days": 14.0,
    # The `api` backend is **stateless**: every round re-sends the whole
    # conversation history. Without a bound, input tokens grow linearly with the
    # round count and total cost is the square of it; once it outgrows the context
    # window it gets a 400 (`invalid_request_error`), and a 400 is not a transient
    # error — all three retries fail with the same over-long history, so that
    # session breaks the same way every round from then on, unless someone knows
    # to issue `/new`. So carry "just the last this many messages"; 0 = no limit
    # (same convention as `dorossi_max_budget_usd`). The `claude_code` side is
    # unaffected (it has `/compact`), and `codex`'s context is on the backend and
    # not carried by us either — this key only constrains `api`.
    "dorossi_api_history_max_msgs": 40,
    # Master switch for Dorossi self-running's "backend self-judges into a loop".
    # True (default) = keep the hybrid trigger (an explicit phrase + backend self-
    # judgement); False = disable only the "self-judge" path, keeping the
    # "explicit phrase" trigger, so the owner can verify in isolation whether
    # self-judgement is a frequency amplifier. Does not affect the phrase fast
    # path.
    "dorossi_self_judge_enabled": True,
    # Ceiling on Dorossi's backend "concurrently running rounds". After
    # parallelisation, different sessions (in practice = different users' active
    # sessions, or the owner's interactive rounds after switching to another slot)
    # can run in parallel; this semaphore caps the number of concurrent backend
    # `claude -p` rounds, so as not to blow up host resources / API concurrency at
    # once. The same session is still serialised by a per-session lock (a hard
    # requirement for `--resume` correctness), independent of this ceiling. Clamps
    # to ≥1 (0 / negative / non-integer → default). Default 3.
    "dorossi_cc_max_parallel": 3,
    # Operational ceiling on the "number of self-running loops in progress at
    # once". This is a **process-count operational valve** (preventing the number
    # of concurrently spawned backend child processes from getting out of hand),
    # **not a spend limit** — the owner has ruled out spend-style limits, and this
    # valve has nothing to do with spend. 0 = no limit (optional); default 3
    # (loose). Independent of dorossi_cc_max_parallel (the semaphore ceiling for
    # interactive rounds): self-running loops are exempt from that semaphore
    # (abort responsiveness takes priority), and this counting valve controls the
    # number of loops running at once instead.
    "dorossi_max_parallel_loops": 3,
    "webrunner_supervisor": _DEFAULT_SUPERVISOR,
    "gui_control": _DEFAULT_GUI_CONTROL,
    "user_roles": _DEFAULT_USER_ROLES,
    "daily_health_report": _DEFAULT_DAILY_HEALTH_REPORT,
    "dashboard": _DEFAULT_DASHBOARD,
    "dorossi_model_check": _DEFAULT_DOROSSI_MODEL_CHECK,
    "platforms": _DEFAULT_PLATFORMS,
}

_VALID_HELP_LANGS = frozenset({"en", "zh-tw", "zh-cn"})
_VALID_DOROSSI_BACKENDS = frozenset({"api", "claude_code"})
_VALID_DOROSSI_CC_TOOLS = frozenset({"off", "full"})
# The floor for the hard ceilings: keeps the Dorossi watchdog hard ceiling from
# being set to 0 / negative / too small and thereby disabling the protection.
# 60s is already far below any normal use, but guarantees the hard tier is
# always a meaningful wall-clock ceiling. The watchdog's other two layers
# (single-turn idle dorossi_cc_idle_limit_sec, self-running silence
# dorossi_loop_silence_limit_sec) share this floor.
DOROSSI_CC_HARD_LIMIT_FLOOR_SEC = 60.0
# The floor for the usage-limit wait seconds. Same reasoning and same number as
# above, but a **different thing**, so it does not share the constant: that one
# is "how long a round may run at most", this one is "the minimum wait before
# retrying after hitting the wall". Here 0 is not "disable the protection" but a
# "hot loop" — the usage-limit detection matches its wording loosely, so the day
# some other error is misdetected, a 0-second wait turns it into a busy loop that
# keeps spawning backend processes. dorossi_backend.DOROSSI_USAGE_WAIT_MIN_SEC is
# the runtime copy of the same number.
DOROSSI_USAGE_WAIT_FLOOR_SEC = 60.0


# The precise "this value was rejected" test.
#
# Each **flat** `_coerce_*` below returns its `default` argument only on
# rejection, and not one of them ever reads that argument's content — so passing
# in a unique sentinel and comparing the return value with `is` distinguishes
# "rejected" from "accepted a value that happens to equal the default" with 100%
# reliability. Verified 2026-09-09 across the eighteen inputs of nine helpers.
#
# **Deliberately not "compare the value" to decide.** `_coerce_help_lang`
# normalises `" ZH-TW "` to `"zh-tw"`, `_coerce_dorossi_cc_tools` normalises
# `"FULL "` to `"full"` — those are **legitimate normalisations**, not
# rejections. Comparing values would misreport them all as "your setting was
# thrown away", and a gate that cries wolf gets switched off sooner or later
# (this project has already narrowed the scan scope of `test_language` and
# `test_text_encoding` for the same reason). The reverse is just as real:
# `presence_probe_interval_sec` written as the string `"8"` is rejected and the
# returned default happens to **also** be 8.0, which comparing values cannot see
# at all.
#
# The five nested-section coercers do not apply: they read `default["..."]` /
# `dict(default)`, and shoving a sentinel in would raise `TypeError` outright.
# Those five use their own small tables and go field-by-field through `_take`.
_REJECTED = object()

# The config-file complaint is printed only once. See the module docstring's
# last paragraph for why (`load_bot_config()` has no cache, and it sits on the
# path of every outbound HTTP request).
#
# Warning dedup moved to `_warn_dedup` (a passive shared module allowed by
# `CLAUDE.md`: pure standard library, importing nothing from the project). There
# used to be a verbatim six-line copy here, and `_batch_config`'s comment said "a
# third copy should be lifted into a shared module" — the third copy appeared.
# Aliasing it to `_warn_once` keeps existing callers unchanged.
# **Dual-shape import, do not collapse to a single bare-name line.** The bare
# name only holds when "`axiomatic/` is itself on `sys.path`" — when running a
# script like `webrunner_*.py` / `discord_bot.py`, `sys.path[0]` happens to be
# the directory they live in, so it works however you run it locally. But
# `start_webrunner.py` lives in the repo root and **deliberately** uses the
# package path `from axiomatic._bot_config import ...` (see its own comment for
# why: the bare-name version only holds at runtime, and a static analyser cannot
# see the `sys.path.insert`). Down that path `axiomatic/` is not on `sys.path`,
# so the bare name is a `ModuleNotFoundError`, and it blows up during the
# launcher's import — the whole webrunner cannot start, rc=1, and restarting
# changes nothing. This actually happened on 2026-09-12. `_external_apis` has had
# this shape from the start.
try:
    from _warn_dedup import warn_once as _warn_once   # noqa: E402
except ImportError:  # package path (`from axiomatic import _bot_config`)
    from axiomatic._warn_dedup import warn_once as _warn_once  # type: ignore  # noqa: E402


def _warn_unknown_keys(raw: dict, known, *, source: str) -> list:
    """Top-level keys present in the config but not recognised by this module →
    warn once. Returns those keys (for the tests).

    **A misspelled key name has the same symptom as a mistyped value type: the
    setting does not take effect.** And this file only takes effect after a
    restart, so the user assumes it applied once they restart. Every `_take`
    above warns on "wrong value", but before this function was added **nothing**
    warned on "wrong key name" — the loader only walks the keys it recognises,
    and never even reads the extras in `raw`.

    Keys starting with `_` are this project's convention for comments in JSON
    (`bot_config.json` now has 16 `*_comment`s), and are not counted as unknown.

    **Print only the key names, never the values.** A value may be a host path
    the user filled in (`gui_control.launch_aliases` is exactly that), and stderr
    goes to the log while `/log tail` sends the log to the chat platform — the
    same reason `_shown()` prints only the type name for containers. The key
    names are truncated and capped in number too: one botched JSON should not
    wash out the whole log.
    """
    unknown = sorted(key for key in raw
                     if key not in known and not str(key).startswith("_"))
    if unknown:
        shown = ", ".join(str(k)[:40] for k in unknown[:8])
        more = "" if len(unknown) <= 8 else f" ({len(unknown) - 8} more)"
        _warn_once(
            f"{source}: unrecognised config keys, ignored: {shown}{more}. "
            "A misspelled key name silently does nothing (the setting never "
            "takes effect) — check the spelling against the defaults table.")
    return unknown


def _shown(value) -> str:
    """How the "received value" is presented in a message.

    A container prints **only the type name, never the content**:
    `gui_control.launch_aliases`'s value is host paths and URIs, and stderr goes
    to `discord_bot.log`, which has `/log tail` as a user-facing exit. That path
    does pass through `_redact_for_discord`, but "safe only because some
    downstream scrubber holds" is exactly the shape this project keeps paying for
    — keep the guarantee local. Scalars do not have this problem: a scalar that
    can be rejected is by definition not a valid value (`api_contact` is rejected
    only when it is **not** a non-empty string, so a real email can never appear
    in the warning).
    """
    if isinstance(value, (list, tuple, dict, set)):
        return type(value).__name__
    return repr(value)


def _coerce_bool(value, default: bool) -> bool:
    """Accept only an actual JSON boolean; anything else (incl. "true"/1) falls
    back to the default, matching the wrong-type → default pattern."""
    if isinstance(value, bool):
        return value
    return default


def _is_finite_number(value) -> bool:
    """The shared precondition for numeric type checks: not a bool, is int/float,
    and **convertible to a finite float**.

    Neither point is obvious from the code alone, but both can arrive from JSON:

    1. **`inf` / `nan`.** `Infinity` / `NaN` are Python's extension to JSON, and
       `json.loads` accepts them by default; and nobody needs to type `Infinity`
       by hand — a completely normal-looking literal like `1e400` parses to
       `inf`. `inf > 0` is true, so any check of the form
       "`isinstance(v, (int, float)) and v > 0`" lets it through. Measured
       consequence: `inter_image_delay_sec: [Infinity, Infinity]` →
       `random.uniform(inf, inf)` returns **nan** (`inf + (inf-inf)*x`) →
       `time.sleep(nan)` raises `ValueError`, and the whole character loop blows
       up at the first inter-image wait. A Dorossi watchdog ceiling that takes
       `inf` amounts to **disabling** the watchdog — exactly what
       `_coerce_clamped_num`'s docstring says must never happen.
    2. **An int too big to become a float.** JSON integers have no upper bound,
       `json.loads` hands back an arbitrary-precision Python int, and both
       `float(10**400)` and `math.isfinite(10**400)` raise `OverflowError`. Both
       loaders' docstrings say "never raises", yet in practice they would —
       `load_bot_config` runs at bot import time, so it means the bot cannot
       start. So block it here with a **comparison** first (comparing an int
       against a float does not overflow), rather than calling `math.isfinite`
       directly.

    `bool` is excluded first: it is a subclass of `int`, and `True` would flow
    through as 1.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        # Comparison only, no float() / math.isfinite() — both overflow on a
        # huge int.
        return -_FLOAT_MAX <= value <= _FLOAT_MAX
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _coerce_int(value, default: int, *, min_value: int = 0) -> int:
    if _is_finite_number(value) and isinstance(value, int) and value >= min_value:
        return value
    return default


def _coerce_positive_num(value, default: float) -> float:
    if _is_finite_number(value) and value > 0:
        return float(value)
    return default


def _coerce_nonneg_num(value, default: float) -> float:
    """Like `_coerce_positive_num` but allows 0 (used for thresholds where
    0 means 'disabled', e.g. min_free_disk_gb)."""
    if _is_finite_number(value) and value >= 0:
        return float(value)
    return default


def _coerce_clamped_num(value, default: float, *, min_value: float) -> float:
    """Coerce a positive number with a hard floor. Used for the Dorossi hard
    watchdog ceilings, where the limit MUST stay meaningful — a value of 0 /
    negative / absurdly small would effectively disable the protection (the
    hard tier exists precisely so a hung tool can't wedge the queue), so we
    clamp up to `min_value` rather than accepting it. A non-number / bool falls
    back to the default.

    **`inf` goes to the default rather than being "accepted as-is"**:
    `inf >= min_value` is true, so accepting it would set the ceiling to infinity
    and effectively disable the watchdog — exactly what this function guards
    against."""
    if not _is_finite_number(value):
        return default
    return float(value) if value >= min_value else min_value


def _coerce_str(value, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default


def _coerce_optional_str(value, default: str) -> str:
    """A string field, but **the empty string is a meaningful valid value**, not
    "left blank".

    Only for `api_contact`: its default is `""`, meaning "no contact info
    attached" (see that key's comment). With `_coerce_str`, `""` would be judged
    a rejection — **behaviourally identical** (the default returned after
    rejection also happens to be `""`), but once per-key warnings were wired up it
    becomes a false warning: the real `bot_config.json` now carries
    `"api_contact": ""`, so every load would accuse the setting of not taking
    effect. This was measured on the spot when warnings were added on 2026-09-09.

    Fixing it in the coercer rather than adding an exception at the warning layer
    is because the problem is here in the first place: `""` is valid for this
    key, and calling it "unusable" is wrong. A gate that cries wolf gets switched
    off sooner or later — this project has already narrowed the scan scope of
    `test_language` and `test_text_encoding` for the same reason.

    The more general rule is pinned by `test_config_numbers`: **no key's own
    default may be rejected by its own coercer**, otherwise you get exactly this
    "scolded for writing the default" false warning.
    """
    if isinstance(value, str):
        return value
    return default


def _coerce_time_str(value, default: str) -> str:
    """`HH:MM` for `daily_health_report.time`.

    Extracted into a coercer **only so it also goes through `_take`** (it used to
    be an inline `if`, so a typo made no sound at all). The validation strength
    is unchanged: non-string / all-whitespace → fall back to the default, the
    rest is stripped as-is. The actual time parsing is on the bot side, and is
    not duplicated here.
    """
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _coerce_help_lang(value, default: str) -> str:
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _VALID_HELP_LANGS:
            return low
    return default


def _coerce_dorossi_backend(value, default: str) -> str:
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _VALID_DOROSSI_BACKENDS:
            return low
    return default


def _coerce_dorossi_cc_tools(value, default: str) -> str:
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _VALID_DOROSSI_CC_TOOLS:
            return low
    return default


def _coerce_gui_control(raw, default: dict) -> dict:
    """The `gui_control` block. `launch_whitelist: list[str]` is the program
    names / paths allowed to launch; `launch_aliases: dict[str, str]` is the
    key → target mapping for `!launch <key>` (target can be a path or a URI).
    Each field uses per-entry validation: a rejected entry does not clear the
    whole section."""
    if not isinstance(raw, dict):
        return {
            "launch_whitelist": list(default["launch_whitelist"]),
            "launch_aliases": dict(default["launch_aliases"]),
        }
    wl = raw.get("launch_whitelist")
    out_wl: list[str] = []
    if isinstance(wl, list):
        for entry in wl:
            if isinstance(entry, str) and entry.strip():
                out_wl.append(entry.strip())
        if len(out_wl) != len(wl):
            # Say only how many, **not the content**: this list holds
            # executable paths on the host.
            _warn_once(f"_bot_config: `gui_control.launch_whitelist` had "
                       f"{len(wl) - len(out_wl)} entries that are not usable "
                       f"strings; skipped those and kept the other "
                       f"{len(out_wl)}")
    elif wl is not None:
        _warn_once(f"_bot_config: `gui_control.launch_whitelist` is not a list "
                   f"(got {_shown(wl)}); ignored, using an empty list")
    al = raw.get("launch_aliases")
    out_al: dict[str, str] = {}
    if isinstance(al, dict):
        for key, value in al.items():
            if (isinstance(key, str) and key.strip()
                    and isinstance(value, str) and value.strip()
                    and not key.startswith("_")):  # auto-skip keys like _comment
                out_al[key.strip()] = value.strip()
        # Underscore-prefixed keys like `_comment` are **deliberately** skipped
        # and do not count as "dropped" — counting them would give every config
        # file with a comment a false warning, exactly the "gate that cries
        # wolf".
        droppable = sum(1 for k in al if not (isinstance(k, str)
                                              and k.startswith("_")))
        if len(out_al) != droppable:
            _warn_once(f"_bot_config: `gui_control.launch_aliases` had "
                       f"{droppable - len(out_al)} entries whose key/target is "
                       f"not a usable string; skipped those and kept the other "
                       f"{len(out_al)}")
    elif al is not None:
        _warn_once(f"_bot_config: `gui_control.launch_aliases` is not a mapping "
                   f"(got {_shown(al)}); ignored, using an empty mapping")
    return {"launch_whitelist": out_wl, "launch_aliases": out_al}


def _coerce_str_list(value, default, *, label: str = "") -> list[str]:
    """A list of platform identifiers (user ids, conversation ids).

    A wrong type for the whole key → the `_take` rejection path (fall back to the
    default and warn); **some items** being unusable → skip them individually,
    say only how many, and do not list the content — these keys hold user and
    conversation ids, printing each one is just noise, and they are other
    people's identifiers. Same stance as `_take_int_list`, only here the values
    are strings.
    """
    if not isinstance(value, list):
        return default
    out: list[str] = []
    for one in value:
        if isinstance(one, bool) or not isinstance(one, (str, int)):
            continue
        text = str(one).strip()
        if text:
            out.append(text)
    if len(out) != len(value) and label:
        _warn_once(f"_bot_config: `{label}` had {len(value) - len(out)} entries "
                   f"that are not usable identifiers; skipped those and kept the "
                   f"other {len(out)}")
    return out


def _coerce_int_list(value) -> list[int]:
    out: list[int] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int) and item >= 0:
            out.append(item)
        elif isinstance(item, str):
            try:
                n = int(item.strip())
            except ValueError:
                continue
            if n >= 0:
                out.append(n)
    return out


def _coerce_user_roles(raw, default: dict) -> dict:
    if not isinstance(raw, dict):
        return {
            "admin_user_ids": list(default["admin_user_ids"]),
            "operator_user_ids": list(default["operator_user_ids"]),
            "viewer_user_ids": list(default["viewer_user_ids"]),
        }
    return {
        key: _take_int_list(raw, key, path="user_roles.")
        for key in ("admin_user_ids", "operator_user_ids", "viewer_user_ids")
    }


def _coerce_daily_health_report(raw, default: dict) -> dict:
    if not isinstance(raw, dict):
        return dict(default)
    return _take_section(raw, default, _DAILY_HEALTH_COERCERS,
                         path="daily_health_report.")


def _coerce_dashboard(raw, default: dict) -> dict:
    if not isinstance(raw, dict):
        return dict(default)
    return _take_section(raw, default, _DASHBOARD_COERCERS, path="dashboard.")


def _coerce_dorossi_model_check(raw, default: dict) -> dict:
    if not isinstance(raw, dict):
        return dict(default)
    return _take_section(raw, default, _DOROSSI_MODEL_CHECK_COERCERS,
                         path="dorossi_model_check.")


def _coerce_platforms(raw, default: dict) -> dict:
    """The `platforms` section: **nested two levels** (section → platform →
    field), so it walks one level itself.

    `_take_section` handles only one level. The extra level is not for looks: the
    next person wiring up a platform adds a **new platform section**, not a pile
    of flat keys with the same prefix, so "which settings this platform has" is
    visible in the file, and an unrecognised platform name has somewhere to be
    caught and warned about.

    It starts from `deepcopy(default)`, so the shape is always complete and the
    return value never shares a container with the module constant —
    `_fallback_bot_config`'s docstring records the cost of that flaw.
    """
    out = copy.deepcopy(default)
    if not isinstance(raw, dict):
        return out
    _warn_unknown_keys(raw, default, source="_bot_config.platforms")
    for name, (table, defaults) in _PLATFORM_COERCERS.items():
        if name not in raw:
            continue
        block = raw[name]
        if not isinstance(block, dict):
            _warn_once(f"_bot_config: `platforms.{name}` is not a config section "
                       f"(got {_shown(block)}); the whole section is ignored, "
                       f"using the default")
            continue
        out[name] = _take_section(block, defaults, table,
                                  path=f"platforms.{name}.")
    return out


def _coerce_supervisor(raw, default: dict) -> dict:
    if not isinstance(raw, dict):
        return dict(default)
    return _take_section(raw, default, _SUPERVISOR_COERCERS,
                         path="webrunner_supervisor.")


# key -> (coercion function, extra keyword args). **Every flat key goes through
# the same path**, no longer one hand-written line per key — so forgetting a
# warning when adding a setting becomes impossible, rather than "remember to add
# it". `test_config_numbers` additionally pins that this table + `_SECTION_KEYS`
# + `_INT_LIST_KEYS` cover `_DEFAULT_BOT_CONFIG` with no gaps or overlaps (pinned
# in both directions).
_COERCERS: dict = {
    "channel_id": (_coerce_int, {"min_value": 0}),
    "owner_user_id": (_coerce_int, {"min_value": 0}),
    # `""` is this key's meaningful "no contact info attached", so it is not
    # `_coerce_str` (see that helper's docstring: the real config file carries
    # `""`, and `_coerce_str` would emit a false warning on every load).
    "api_contact": (_coerce_optional_str, {}),
    "target_presence_username": (_coerce_optional_str, {}),
    "default_help_lang": (_coerce_help_lang, {}),
    "presence_probe_interval_sec": (_coerce_positive_num, {}),
    "event_poll_seconds": (_coerce_positive_num, {}),
    "alert_user_id": (_coerce_int, {"min_value": 0}),
    "min_free_disk_gb": (_coerce_nonneg_num, {}),
    "keep_bot_awake": (_coerce_bool, {}),
    "dorossi_backend": (_coerce_dorossi_backend, {}),
    "dorossi_cc_tools": (_coerce_dorossi_cc_tools, {}),
    "dorossi_cc_hard_limit_off_sec": (
        _coerce_clamped_num, {"min_value": DOROSSI_CC_HARD_LIMIT_FLOOR_SEC}),
    "dorossi_cc_hard_limit_full_sec": (
        _coerce_clamped_num, {"min_value": DOROSSI_CC_HARD_LIMIT_FLOOR_SEC}),
    "dorossi_cc_idle_limit_sec": (
        _coerce_clamped_num, {"min_value": DOROSSI_CC_HARD_LIMIT_FLOOR_SEC}),
    "dorossi_loop_silence_limit_sec": (
        _coerce_clamped_num, {"min_value": DOROSSI_CC_HARD_LIMIT_FLOOR_SEC}),
    # Usage-limit wait strategy. Both second values clamp to ≥
    # DOROSSI_USAGE_WAIT_FLOOR_SEC (60s): this is spin protection and cannot be
    # disabled via config — the usage-limit detection matches its wording
    # loosely, and allowing a 0-second wait on a misdetection would become a hot
    # loop. max_consecutive allows 0 (= no limit).
    "dorossi_usage_wait_fallback_sec": (
        _coerce_clamped_num, {"min_value": DOROSSI_USAGE_WAIT_FLOOR_SEC}),
    "dorossi_usage_wait_max_sec": (
        _coerce_clamped_num, {"min_value": DOROSSI_USAGE_WAIT_FLOOR_SEC}),
    "dorossi_transient_max_consecutive": (_coerce_int, {"min_value": 0}),
    "dorossi_error_retry_max": (_coerce_int, {"min_value": 0}),
    "dorossi_silence_retry_max": (_coerce_int, {"min_value": 0}),
    "dorossi_usage_wait_max_consecutive": (_coerce_int, {"min_value": 0}),
    # The two valves for auto-resume across a restart; both allow 0 (= off / no
    # limit), so they are min 0 rather than clamped to a floor. Age uses
    # nonneg_num (a non-finite value is pushed back to the default).
    "dorossi_loop_autoresume_max_age_sec": (_coerce_nonneg_num, {}),
    "dorossi_loop_autoresume_max_tries": (_coerce_int, {"min_value": 0}),
    # 0 = disabled (uses _coerce_nonneg_num, allowing 0, like min_free_disk_gb).
    "dorossi_max_budget_usd": (_coerce_nonneg_num, {}),
    # Periodic-compaction thresholds; each 0 = that condition disabled (both
    # allow 0, using nonneg / int min 0).
    "dorossi_loop_compact_every_rounds": (_coerce_int, {"min_value": 0}),
    "dorossi_loop_compact_cost_usd": (_coerce_nonneg_num, {}),
    # Token threshold for compact-when-context-too-big (shared by single-turn +
    # self-running); 0 = disabled (allows 0, min 0).
    "dorossi_compact_context_tokens": (_coerce_int, {"min_value": 0}),
    # 0 = disabled (auto-reset only when clearly too old, conservative).
    "dorossi_session_max_age_days": (_coerce_nonneg_num, {}),
    "dorossi_api_history_max_msgs": (_coerce_int, {"min_value": 0}),
    "dorossi_self_judge_enabled": (_coerce_bool, {}),
    # Ceiling on concurrent backend rounds; clamp to ≥1 (the semaphore must at
    # least admit one round).
    "dorossi_cc_max_parallel": (_coerce_int, {"min_value": 1}),
    # Operational valve on self-running loop concurrency; 0 = no limit (allows 0,
    # min 0). Not a spend limit.
    "dorossi_max_parallel_loops": (_coerce_int, {"min_value": 0}),
}

# Field tables for the nested sections, same shape as `_COERCERS`. Made data for
# the same reason: `test_config_numbers` pins that each table covers its own
# `_DEFAULT_*`, so adding a section field but forgetting to attach a coercion
# (= no validation at all, and no warning) goes straight red.
_SUPERVISOR_COERCERS: dict = {
    "fallback_window_sec": (_coerce_int, {}),
    "respawn_backoff_min_sec": (_coerce_positive_num, {}),
    "respawn_backoff_max_sec": (_coerce_positive_num, {}),
    "healthy_threshold_sec": (_coerce_positive_num, {}),
    "rapid_fail_threshold_sec": (_coerce_positive_num, {}),
    "rapid_fail_giveup_count": (_coerce_int, {"min_value": 1}),
    "zero_progress_giveup_count": (_coerce_int, {"min_value": 1}),
    "oneshot_retry_giveup_count": (_coerce_int, {"min_value": 1}),
    # `_coerce_positive_num` guarantees > 0, which incidentally satisfies the
    # `0 < minimum` half of `restart_backoff`'s precondition (the other half,
    # max < min, is clamped at the call site).
    "oneshot_retry_backoff_min_sec": (_coerce_positive_num, {}),
    "oneshot_retry_backoff_max_sec": (_coerce_positive_num, {}),
}

_DAILY_HEALTH_COERCERS: dict = {
    "enabled": (_coerce_bool, {}),
    "channel_id": (_coerce_int, {"min_value": 0}),
    "time": (_coerce_time_str, {}),
}

_DASHBOARD_COERCERS: dict = {
    "host": (_coerce_str, {}),
    "port": (_coerce_int, {"min_value": 1}),
}

_DOROSSI_MODEL_CHECK_COERCERS: dict = {
    "enabled": (_coerce_bool, {}),
    # Positive (`_coerce_positive_num`) = cannot be set to 0 or negative. 0 would
    # make the check run every minute, which is not "off" but "always running" —
    # to turn it off use `enabled`.
    "interval_hours": (_coerce_positive_num, {}),
    "announce_channel_id": (_coerce_int, {"min_value": 0}),
}

_TELEGRAM_COERCERS: dict = {
    "enabled": (_coerce_bool, {}),
    "owner_user_ids": (
        _coerce_str_list, {"label": "platforms.telegram.owner_user_ids"}),
    "allowed_chat_ids": (
        _coerce_str_list, {"label": "platforms.telegram.allowed_chat_ids"}),
    # Positive: 0 would become a no-wait hot loop (each round returns instantly
    # and asks again), which is not "off". To turn it off use `enabled` — same
    # reasoning as `dorossi_model_check.interval_hours`.
    "poll_timeout_sec": (_coerce_positive_num, {}),
}

# Platform name → (field table, that platform's defaults). **Add a platform, add
# a row**, and `test_platform_transports` reconciles this table against
# `_DEFAULT_PLATFORMS` in both directions: a missing row means that platform is
# not validated at all and stays silent, and an extra row becomes a `KeyError`
# in `_take`.
_DISCORD_PLATFORM_COERCERS: dict = {
    "enabled": (_coerce_bool, {}),
}

_PLATFORM_COERCERS: dict = {
    "discord": (_DISCORD_PLATFORM_COERCERS, _DEFAULT_PLATFORM_DISCORD),
    "telegram": (_TELEGRAM_COERCERS, _DEFAULT_PLATFORM_TELEGRAM),
}

# Nested sections (each with its own coercer) and int-list keys
# (`_coerce_int_list` has no `default` parameter, so the sentinel trick does not
# apply). Neither is in `_COERCERS`, but both must be covered — these two tuples
# are the "already thought about it" record, and the tests reconcile them against
# `_DEFAULT_BOT_CONFIG`.
_SECTION_KEYS = ("webrunner_supervisor", "gui_control", "user_roles",
                 "daily_health_report", "dashboard", "dorossi_model_check",
                 "platforms")
_INT_LIST_KEYS = ("path_reveal_channel_ids",)


def _take(raw: dict, defaults: dict, key: str, coerce, kwargs: dict | None = None,
          *, path: str = ""):
    """Read `raw[key]`, apply its coercion, **and say something when the value is
    thrown away or raised by a floor**.

    "This key was simply not written" is the normal case and must be completely
    silent — so check `key in raw` first, rather than `raw.get(key)` and then
    judging (that way a key written as `null` would look like an absent key, and
    the former is a genuine user typo).
    """
    if key not in raw:
        return defaults[key]
    value = raw[key]
    kwargs = kwargs or {}
    got = coerce(value, _REJECTED, **kwargs)
    label = path + key
    if got is _REJECTED:
        _warn_once(f"_bot_config: `{label}` has an unusable value "
                   f"(got {_shown(value)}); ignored, using the default "
                   f"{defaults[key]!r}")
        return defaults[key]
    if coerce is _coerce_clamped_num:
        # **A "clamp" is not a "rejection", and the messages must be kept
        # apart.** The user explicitly wrote 10 and it actually runs 60 — that is
        # not a normalisation, it is their intent being changed — so it warns
        # too, but says a different thing: the value is legal, only below a
        # protection floor that cannot be disabled.
        #
        # The decision **does not compare the result with the input**:
        # `float(2**53 + 1) != 2**53 + 1`, which would misreport a huge but legal
        # integer as clamped. Instead it asks the same coercer again with only
        # the floor removed (`-_FLOAT_MAX` rather than `-inf`: `_is_finite_number`
        # already guarantees the value is within this range, and this module
        # deliberately keeps inf out of every comparison) — a different answer
        # means the floor took effect. Using the coercer itself as the yardstick
        # keeps this from drifting away from its implementation.
        without_floor = coerce(value, _REJECTED,
                               **{**kwargs, "min_value": -_FLOAT_MAX})
        if got != without_floor:
            _warn_once(f"_bot_config: `{label}` got {_shown(value)}, below the "
                       f"floor; raised to {got!r} — this is a protection (cannot "
                       f"be disabled via config), not a setting that failed to "
                       f"apply")
    return got


def _take_section(raw: dict, defaults: dict, table: dict, *, path: str) -> dict:
    """The field-by-field version for a nested section.

    It starts from `dict(defaults)` rather than assembling only the keys in the
    table: the shape always matches the default, and even if the table falls
    behind `_DEFAULT_*` some day, callers never hit a `KeyError` (that guard is
    in the tests; this is the second layer).
    """
    out = dict(defaults)
    for key, (coerce, kwargs) in table.items():
        out[key] = _take(raw, defaults, key, coerce, kwargs, path=path)
    return out


def _take_int_list(raw: dict, key: str, *, path: str = "") -> list[int]:
    """The path for `path_reveal_channel_ids` and the three `*_user_ids`.

    `_coerce_int_list` has no `default` parameter (its "default" is always the
    empty list), so the sentinel trick above does not apply. It uses two direct
    checks instead, each matching a kind of silent failure:

    * **Wrong type for the whole key** → empty list. The direction is safe
      (fail-closed: fall back to "only DMs see the full path" / "the role system
      is not configured"), but the user thinks they enabled that permission, and
      a fail-closed failure is exactly the kind nobody notices.
    * **Some items dropped** → say only **how many**, not the content: these keys
      hold user / channel IDs, and listing each one is just noise.
    """
    if key not in raw:
        return []
    value = raw[key]
    out = _coerce_int_list(value)
    label = path + key
    if not isinstance(value, list):
        _warn_once(f"_bot_config: `{label}` is not a list (got {_shown(value)}); "
                   f"ignored, using an empty list")
    elif len(out) != len(value):
        _warn_once(f"_bot_config: `{label}` had {len(value) - len(out)} entries "
                   f"that are not usable IDs; skipped those and kept the other "
                   f"{len(out)}")
    return out


def _section(raw: dict, name: str):
    """Pull out a nested section and hand it to its coercer.

    Key not written → `None` (the normal case, nothing printed); written but
    **not a section** → warn, and still return `None` so the coercer takes its
    existing "whole section uses the default" branch. Behaviour is exactly the
    same as the original `raw.get(name)` (a non-dict likewise lands on
    `not isinstance(raw, dict)`); the only difference is whether it makes a
    sound.
    """
    if name not in raw:
        return None
    value = raw[name]
    if isinstance(value, dict):
        return value
    _warn_once(f"_bot_config: `{name}` is not a config section "
               f"(got {_shown(value)}); the whole section is ignored, using the "
               f"default")
    return None


def _fallback_bot_config() -> dict:
    """The full default config returned when `bot_config.json` cannot be read /
    parsed.

    **The shape must be exactly the same as the normal merge path.** With one key
    missing, the caller would hit a `KeyError` at the very moment the config file
    just broke — which is precisely the worst moment for a second failure (the
    bot must still start when the config file is broken, and that is the entire
    reason this fallback exists).

    This literal used to be copied into each of three `except` branches. Adding a
    nested default key meant remembering to change three places, and missing one
    **turned no test red**, and was only reachable when the config file was
    broken, so it could never be caught in normal testing.

    It uses `deepcopy` rather than the original per-key shallow copy, because
    `dict(_DEFAULT_USER_ROLES)` copies only the outer layer: the three id lists
    would still be **the same object** as the module constant, so one caller
    `append` permanently pollutes the default. This is not theory —
    `_roles_configured()` decides "the role system is not configured" precisely
    by "all three lists empty", and polluting it would make the permission gate
    spontaneously become "configured" on the next load.
    """
    return copy.deepcopy(_DEFAULT_BOT_CONFIG)


def load_bot_config() -> dict:
    """Return a fully-populated bot config dict, using defaults for any
    missing / malformed key. Always succeeds; never raises."""
    raw: dict = {}
    try:
        text = BOT_CONFIG_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _fallback_bot_config()
        # `UnicodeDecodeError` is a subclass of `ValueError`, **not** `OSError`:
        # a config file another editor re-saved as Big5 would escape here, and
        # the local locale is exactly cp950 while the file content is almost all
        # Chinese. It is not silently swallowed with `errors="replace"` — that
        # would use mojibake as valid values, worse than falling back to the
        # default.
    except (OSError, UnicodeDecodeError) as error:
        print(f"_bot_config: read failed: {error!r}", file=sys.stderr)
        return _fallback_bot_config()
    try:
        parsed = json.loads(text or "{}")
        if isinstance(parsed, dict):
            raw = parsed
    except json.JSONDecodeError as error:
        print(f"_bot_config: parse failed: {error!r}", file=sys.stderr)
        return _fallback_bot_config()

    # Start from the defaults and overwrite key by key, so the shape is always
    # complete (the caller never hits a `KeyError` at the moment the config file
    # just broke). **The three blocks below must cover `_DEFAULT_BOT_CONFIG` with
    # no gaps or overlaps**: missing a key is not just "that key is not
    # validated", it also leaves `cfg` holding **the same** module-constant
    # container from the shallow copy — `_fallback_bot_config`'s docstring records
    # the cost of that flaw (one caller `append` permanently pollutes the
    # default, and the permission gate spontaneously becomes "configured").
    # `test_config_numbers` pins this in both directions.
    _warn_unknown_keys(raw, _DEFAULT_BOT_CONFIG, source="_bot_config")

    cfg = dict(_DEFAULT_BOT_CONFIG)
    for key, (coerce, kwargs) in _COERCERS.items():
        cfg[key] = _take(raw, _DEFAULT_BOT_CONFIG, key, coerce, kwargs)
    # Channel ID lists; bad values are dropped one by one, and an absent /
    # wrong-type whole key → empty list, i.e. fail-closed back to "only DMs see
    # the full path".
    for key in _INT_LIST_KEYS:
        cfg[key] = _take_int_list(raw, key)
    # Nested sections. `_section()` does one extra thing: warn when the whole
    # section has the wrong type (the original `raw.get(name)` swallowed a botched
    # section whole, without a word).
    cfg["webrunner_supervisor"] = _coerce_supervisor(
        _section(raw, "webrunner_supervisor"), _DEFAULT_SUPERVISOR)
    cfg["gui_control"] = _coerce_gui_control(
        _section(raw, "gui_control"), _DEFAULT_GUI_CONTROL)
    cfg["user_roles"] = _coerce_user_roles(
        _section(raw, "user_roles"), _DEFAULT_USER_ROLES)
    cfg["daily_health_report"] = _coerce_daily_health_report(
        _section(raw, "daily_health_report"), _DEFAULT_DAILY_HEALTH_REPORT)
    cfg["dashboard"] = _coerce_dashboard(
        _section(raw, "dashboard"), _DEFAULT_DASHBOARD)
    cfg["dorossi_model_check"] = _coerce_dorossi_model_check(
        _section(raw, "dorossi_model_check"), _DEFAULT_DOROSSI_MODEL_CHECK)
    cfg["platforms"] = _coerce_platforms(
        _section(raw, "platforms"), _DEFAULT_PLATFORMS)
    return cfg
