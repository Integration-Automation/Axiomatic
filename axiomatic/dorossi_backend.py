"""Dorossi backend: Claude Code / Anthropic API invocation, session persistence and pure logic (P5 refactor).

P5 refactor: extract the **backend and pure logic** of `@bot Dorossi` out of
`discord_bot.py`. The criterion is the same as P1/P2/P4 -- "returns data / pure
logic / backend invocation not bound to discord objects" moves out, while
"orchestration that produces a Discord reply, or is entangled with bot runtime
state (locks, queues, the autonomous-loop global flag, live streaming messages,
the discord channel)" stays in the bot.

Moved into this module (no discord, no mutable bot globals):
  * Dorossi backend configuration constants (derived from bot_config), the
    system prompt, the autonomous-loop prompt / sentinel corpus, and intent
    phrase matching.
  * Multi-session storage (load / save / migrate / accessor helpers, pure JSON
    logic).
  * The backend invocation itself: claude_code (`claude -p` streaming + a
    two-stage watchdog + an autonomous-output silence backstop + a budget gate)
    and the Anthropic API; plus pure decision helpers for usage-limit / budget /
    round-info / compaction triggering.
  * Three typed exceptions (_DorossiResumeError / _DorossiLoopSilence /
    _DorossiUsageLimitError), raised by the backend invocation and caught by the
    bot's orchestration.

Left in `discord_bot.py` (entangled with discord / runtime): mcmd_dorossi /
_dorossi_process_turn / _dorossi_run_loop / _dorossi_loop_one_round / mcmd_session /
mcmd_abort, the live stream (_DorossiLiveMessage), the queue lock / waiter / the
autonomous-loop global flag / the mid-flight injection buffer, Discord reply
assembly (_dorossi_error_hint / _dorossi_usage_limit_reply /
_dorossi_render_session_list / _dorossi_apply_session_action), the owner gate
(_dorossi_loop_gate_open / _dorossi_should_loop), DOROSSI_USER_ID / OWNER_USER_ID.

Module boundary (CLAUDE.md hard rule): a passive shared module, importable by
the bot; it MUST NOT `import discord_bot` (circular), and MUST NOT import the
webrunner scripts. This module never touches discord and never assembles any
reply string bound for Discord -- on failure it always prints to stderr and
returns data / an answer or raises a typed exception, leaving the bot to
assemble a generic reply. The token-cost rules (B1/B2/B3) and the autonomous
invariants are all maintained here in the backend invocations.
"""
from __future__ import annotations

import asyncio
import email.utils
import hashlib
import json as _json
import math
import os
import re
import shutil as _shutil
import sys
import time
from pathlib import Path

# Optional dependency: the API backend talks to the Anthropic SDK. Guarded so a
# fresh clone without `anthropic` still imports — the api path degrades to a
# "not installed" error that the bot reports generically.
try:
    import anthropic
    from anthropic import AsyncAnthropic
except Exception:  # pragma: no cover - optional dependency
    anthropic = None
    AsyncAnthropic = None

from _bot_config import load_bot_config
# Externalised prompt-text loader (passive, stdlib-only). The long prompt strings
# live under `bot_prompts/` in the repo root, read once at startup; a missing /
# corrupt file falls back to the built-in `_DEFAULT_*` defaults below, so a fresh
# clone always starts. The files use {sentinel}/{open_sentinel} placeholders that
# `replacements` swaps back to the in-code sentinel constants at load time (the
# sentinels stay single-sourced in code).
from _bot_prompts import load_prompt
# "print the same text to stderr only once" (a passive shared module). The
# backend CLI's environment- and launch-shape warnings are re-evaluated every
# round; without dedup an unchanged environment variable would wash out the whole
# log with the same line.
from _warn_dedup import warn_once as _warn_once
# This process's platform identity. The sessions, usage log, model catalogue and
# working directory are all **this process's own** state, so they always land
# under `state/<platform>/` -- one process per platform, sharing no mutable state
# with each other, and therefore needing no cross-process lock.
from _platform_runtime import platform_file as _platform_state

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Dorossi backend tunables live in bot_config.json (loaded once at import; the
# bot loads its own copy too — same JSON, same values; `!restart` to apply).
BOT_CONFIG = load_bot_config()


DOROSSI_BACKEND = BOT_CONFIG["dorossi_backend"]  # "claude_code" | "api"
# Tool mode for the claude_code backend (see bot_config.json). "off" (the
# default) = plain chat with every tool disabled; "full" = a complete agent
# (bypassPermissions + all tools).
DOROSSI_CC_TOOLS = BOT_CONFIG["dorossi_cc_tools"]  # "off" | "full"
# Don't cap answer length. The Claude Code backend has no token cap; for the
# API backend keep a generous non-streaming ceiling (~16K stays under the SDK
# HTTP-timeout guard). Long answers are split across Discord messages, not cut.
DOROSSI_MAX_TOKENS = 16000
DOROSSI_MODEL = "claude-opus-4-8"          # API backend model id
DOROSSI_CC_MODEL = "opus"                  # Claude Code backend model alias
# Valid values for the tuning commands (`/effort`, `/model`). The user can set
# thinking effort and the backend model with these two commands at the **start**
# of a prompt; they are stripped from the prompt actually sent to the backend
# after parsing. **Session-level semantics (owner ruling, replacing the original
# per-turn design)**: once set, the value is written to that session's persistent
# record (a slot in dorossi_session.json, keys tune_effort / tune_model) and is
# reused for that round and every later round (follow-up, resume retry, each
# autonomous round, compaction round) until a new command overrides it;
# `/effort default` / `/model default` clears the override and returns to the
# default. A new context started by `/new` / reset begins from the default
# (_dorossi_reset_session also clears tune_*); each session stores its own value
# independently, so switching sessions switches the tuning.
# The five effort values are exposed directly (all generic vocabulary, referring
# to no backend).
DOROSSI_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# Valid values for `/model` (an allowlist). **A narrow-scope secrecy exception by
# owner ruling (2026-07-02)**: this `/model` surface (the help list of valid
# values, error hints, the model display in the session list) may expose the
# "backend model alias" directly, no longer abstracted behind a fast/standard/max
# generic tier -- for this one surface only. Every other secrecy rule (never
# reveal the CLI wiring, paths, raw errors or other service names) stays as-is,
# and this exception must not be extrapolated to any other surface. key = the
# externally shown value = the backend model **alias**; value = the value
# actually passed to the CLI. **The allowlist validation must not be removed**:
# user input is never dropped verbatim into a CLI argument, only a key that hits
# the lookup is stored / sent. When unspecified, the DOROSSI_CC_MODEL default
# holds. The session store keeps the key; on read it is validated against the
# table (_dorossi_session_tuning), and a hand-edited store / a key removed from
# the table falls back to the default automatically without breaking.
#
# **2026-09-12: key and value are no longer identity-equal** (originally all four
# keys were identity mappings, and this comment already said back then "to pin to
# a full model id later, change only the value" -- now done). The user must be
# able to pick a version, not only get "the current newest one in that family".
# The two kinds of key deliberately coexist:
#
#   * **The four without a version** (`opus` / `sonnet` / `haiku` / `fable`) keep
#     a bare alias as the value, meaning "the **current newest** one in this
#     family" -- resolved by the backend itself, so a newly released model is
#     tracked without touching this table. Pick this to always get the latest.
#   * **The versioned ones** are pinned to a full model id, meaning "exactly this
#     version, untouched by upgrades". Pick this when a long-running task needs
#     reproducibility.
#
# **The value side (the full model id) is never sent to the chat platform.**
# External display always goes through `discord_bot._model_alias_for()`, which
# reverse-maps value -> key; the secrecy ruling (2026-07-02) permits the
# **alias**, not the full id, so the key must keep its alias shape and a model id
# must never be used directly as a key.
#
# **This table once had an external ceiling: a slash command's static menu holds
# at most 25 options** (a chat-platform limit). Since 2026-09-23 `/dorossi model`
# uses autocomplete instead (see `discord_bot` `_dorossi_model_autocomplete`), so
# the ceiling is only "at most 25 per **single response**" and no longer falls on
# this table -- because the model catalogue below merges newly discovered aliases
# in at runtime, and a static menu cannot keep up with a moving table. This table
# itself may keep growing.
#
# The version strings themselves are copied from the backend CLI's model
# catalogue (`--model` accepts either an "alias" or a "full name"; a string not
# in the catalogue is rejected on the spot by the CLI as `unrecognized_model`,
# not silently dropped to the default).
DOROSSI_MODEL_CHOICES = {
    "opus": "opus",
    "opus-5": "claude-opus-5",
    "opus-4.8": "claude-opus-4-8",
    "opus-4.7": "claude-opus-4-7",
    "opus-4.6": "claude-opus-4-6",
    "sonnet": "sonnet",
    "sonnet-5": "claude-sonnet-5",
    "sonnet-4.6": "claude-sonnet-4-6",
    "sonnet-4.5": "claude-sonnet-4-5",
    "haiku": "haiku",
    "haiku-4.5": "claude-haiku-4-5",
    "fable": "fable",
    "fable-5.1": "claude-fable-5-1",
    "fable-5": "claude-fable-5",
}
# A "read-time" mapping from legacy tier keys (from the generic-tier-abstraction
# era) possibly already in a session store -> the new aliases, so an existing
# session's setting migrates transparently (a fallback on the read side only,
# never rewriting the store; the next command naturally overrides the old key).
DOROSSI_LEGACY_MODEL_KEYS = {
    "fast": "haiku",
    "standard": "sonnet",
    "max": "opus",
}
# The tuning commands' "clear" literal: `/effort default` / `/model default`
# clears that session's override value and returns to the default (effort has no
# "no-flag" literal to type, so it needs a clear syntax).
DOROSSI_TUNE_DEFAULT = "default"

# ---- One model table per backend (2026-09-23) ------------------------------
# The table above is the claude family's, shared by the `claude_code` and `api`
# backends. **The codex backend's models are another vendor's names, and one
# table cannot serve both** -- before this, `/model` had no effect on codex at
# all (argv did not even carry `-m`), and the display side only said "the
# backend's default model", so the user could not tell their chosen value was
# being dropped.
#
# The contents of this codex table are **measured, not guessed**. On 2026-09-23
# the local CLI was tested against this account with four common names
# (`gpt-5.1-codex`, `gpt-5.1-codex-max`, `gpt-5-codex`, `gpt-5.6-sol-mini`), and
# all four were rejected by the server with a 400 "not supported when using Codex
# with a ChatGPT account"; the CLI's own config file also listed only one usable
# model. **The cost of guessing a name is that the user can select it but only
# fails at the next prompt**, seeing only a generic error -- exactly the silence
# this change is fixing. So the table holds only the one confirmed-usable model
# and leaves the rest for the daily model-catalogue check to discover (see
# `dorossi_refresh_model_catalog`).
#
# The naming rule is the same shape as the claude table, in the opposite
# direction: codex's full id is "vendor-version-family" (`gpt-5.6-sol`), so the
# alias is "family-version" (`sol-5.6`). Aliases **never contain a vendor word**
# -- the secrecy ruling permits the alias, not the full id, and not the vendor
# prefix.
DOROSSI_CODEX_MODEL_CHOICES = {
    "sol": "sol",
    "sol-5.6": "gpt-5.6-sol",
}

# backend id -> its model table. `/model` offers and accepts only values the
# current backend can take.
DOROSSI_BACKEND_MODEL_CHOICES = {
    "claude_code": DOROSSI_MODEL_CHOICES,
    "api": DOROSSI_MODEL_CHOICES,
    "codex": DOROSSI_CODEX_MODEL_CHOICES,
}
# Model-catalogue namespaces: the two backends share the claude table, so they
# also share the same discovery results.
DOROSSI_MODEL_NAMESPACES = {
    "claude_code": "claude", "api": "claude", "codex": "codex",
}
# **Only this backend's own CLI understands a bare alias** (`--model opus` = "the
# current newest in this family"). The other two need a full id: api passes the
# string straight through as the model parameter into the SDK, and codex sends it
# straight to the server (measured: an unrecognised name is not dropped to the
# default but returns a 400). So a bare alias must first be turned into a
# concrete id by the model catalogue for those two backends -- the second reason
# the daily check exists.
DOROSSI_BACKENDS_RESOLVING_ALIASES = frozenset({"claude_code"})

# ---- Runtime model catalogue (written by the daily check, merged back into the
# built-in table at load) ----------------------------------------------------
# The built-in table is the **floor**: a fresh clone without this file still has
# a usable set of aliases. What the daily check discovers only **adds** aliases,
# never overwriting the built-in ones.
DOROSSI_MODEL_CATALOG_FILE = _platform_state(PROJECT_ROOT / "dorossi_models.json")
DOROSSI_MODEL_CATALOG_SCHEMA = 1
# The family names used for probing: a bare alias is sent to the CLI and what it
# **resolves to** is read back -- that is "today's newest in this family". The
# family names are taken from the version-less keys in the built-in table rather
# than hand-writing a second copy.
DOROSSI_MODEL_PROBE_FAMILIES = tuple(
    key for key, value in DOROSSI_MODEL_CHOICES.items() if key == value)

# The vendor prefixes on a full model id. An alias always strips it.
_DOROSSI_MODEL_VENDOR_PREFIXES = ("claude", "gpt")
_DOROSSI_MODEL_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")


def _dorossi_split_model_id(model_id) -> tuple:
    """Full model id -> `(family, version)`; returns `(None, None)` if unparsable.

    The two vendors' id shapes differ (`claude-opus-5-5` is "vendor-family-
    version", `gpt-5.6-sol` is "vendor-version-family"), but the **positions**
    differ while the **components** are the same: after stripping the vendor
    prefix, the purely numeric segment is the version and the rest is the family.
    So this does not split by position but classifies by component, and both
    vendors share one implementation.

    Pure function, never raises (the input is a string printed by another
    process).
    """
    text = str(model_id or "").strip().lower()
    if not text or not re.fullmatch(r"[a-z0-9.\-]+", text):
        return (None, None)
    parts = [p for p in text.split("-") if p]
    if parts and parts[0] in _DOROSSI_MODEL_VENDOR_PREFIXES:
        parts = parts[1:]
    version = [p for p in parts if _DOROSSI_MODEL_VERSION_RE.fullmatch(p)]
    family = [p for p in parts if not _DOROSSI_MODEL_VERSION_RE.fullmatch(p)]
    if not family:
        return (None, None)
    return ("-".join(family), ".".join(version) if version else None)


def dorossi_model_alias_from_id(model_id) -> str | None:
    """Full model id -> the **alias** (`claude-opus-5-5` -> `opus-5.5`); None if
    unparsable.

    This is the entry point for display and announcements: when a new model
    appears, only the alias can be shown externally, and not one character of the
    full id is ever sent out.
    """
    family, version = _dorossi_split_model_id(model_id)
    if not family:
        return None
    return f"{family}-{version}" if version else family


def dorossi_model_id_from_alias(namespace: str, alias: str) -> str | None:
    """Alias -> full model id (the **forward** direction of each table's naming
    rule); None if there is no version.

    There is only one reason it exists: to **verify the round trip** before
    merging the catalogue. A new alias is merged into the table only when
    `alias -> id` reproduces the original id. Without this step an unseen naming
    shape (e.g. the version placed before the family) would be merged as an entry
    that **cannot be derived back** -- the allowlist would still admit it, the
    value would still be stored in the session, and it would only be rejected by
    the backend at the next prompt, while the user sees only a generic failure
    message. `test_dorossi_tuning.test_every_value_is_derivable_from_its_own_alias`
    pins exactly this rule, and the round-trip check makes every merged entry
    satisfy it **by construction**.
    """
    if not isinstance(alias, str) or "-" not in alias:
        return None
    family, _, version = alias.rpartition("-")
    if not family or not _DOROSSI_MODEL_VERSION_RE.fullmatch(version):
        return None
    if namespace == "claude":
        return "claude-" + alias.replace(".", "-")
    if namespace == "codex":
        return f"gpt-{version}-{family}"
    return None


def dorossi_load_model_catalog() -> dict:
    """Read the runtime model catalogue. Never raises -- a missing / corrupt file
    just means "not checked yet"."""
    try:
        text = DOROSSI_MODEL_CATALOG_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] model catalog load failed: {exc!r}", file=sys.stderr)
        return {}
    try:
        raw = _json.loads(text)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] model catalog corrupt, ignoring: {exc!r}",
              file=sys.stderr)
        return {}
    return raw if isinstance(raw, dict) else {}


def dorossi_save_model_catalog(catalog: dict) -> bool:
    """Atomic write (same-directory temp -> `os.replace`). Returns whether the
    write succeeded; never raises.

    Both reader and writer are the bot itself; it is on the atomic-write list to
    **survive a restart**: a half-written file read at the next startup is a
    catalogue that silently falls back to the built-in table, and the user only
    notices that "a model I could pick yesterday is gone today", with no error
    message at all.
    """
    try:
        tmp = DOROSSI_MODEL_CATALOG_FILE.with_name(
            DOROSSI_MODEL_CATALOG_FILE.name + ".tmp")
        tmp.write_text(_json.dumps(catalog, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, DOROSSI_MODEL_CATALOG_FILE)
        return True
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] model catalog save failed: {exc!r}", file=sys.stderr)
        return False


# The union of the two tables. `_dorossi_parse_turn_flags` is a **pure function**
# that cannot touch a session, so it cannot know which backend this round is on
# -- its job is "user input never enters a CLI argument verbatim", for which
# validating against the union is enough; "is this value usable on this backend"
# is decided by `dorossi_model_applies` where a session is available, and is
# **stated out loud** (see `discord_bot._dorossi_tuning_labels`).
#
# ⚠️ **This is a dict updated in place, not a snapshot -- and that distinction is
# measured.** The first version wrote `{**A, **B}` computed once at import, so an
# alias merged into the tables by the daily check at runtime was **not in the
# union**: `_model_alias_for`'s reverse lookup missed ⇒ the announcement filtered
# out the just-discovered alias entirely (measured: the first real run announced
# neither of the two new aliases), and the token path `@bot /model <new alias>`
# was rejected as an invalid value -- selectable in the menu yet unrecognised
# when typed. So `dorossi_merge_model_catalog` must rebuild it after every merge,
# and rebuild it **in place** (`clear()` + `update()`): `discord_bot` imports it
# by name, so reassigning a new dict only swaps the name here while the bot still
# holds the old one.
DOROSSI_ALL_MODEL_CHOICES: dict = {}


def _dorossi_rebuild_all_model_choices() -> None:
    """Rebuild the union in place (reason in the warning above). The claude table
    comes first, keeping the existing enumeration order."""
    DOROSSI_ALL_MODEL_CHOICES.clear()
    DOROSSI_ALL_MODEL_CHOICES.update(DOROSSI_MODEL_CHOICES)
    DOROSSI_ALL_MODEL_CHOICES.update(DOROSSI_CODEX_MODEL_CHOICES)


def dorossi_merge_model_catalog(catalog: dict) -> list:
    """Merge the model ids discovered in the catalogue into the built-in tables
    (add only, never overwrite), returning the **newly added aliases**.

    The return value feeds the announcement, so it is only ever aliases. The
    merge has three gates: the alias must be derivable, it must round-trip, and
    the table must not already hold this alias or this value -- the third gate
    blocks "the same id coming in again under a different alias", which would make
    `_model_alias_for`'s reverse lookup pick one of the two aliases while the
    other thereafter displays as someone else's name. The only side effects are
    "those two tables grow in place" and "the union is rebuilt to match"; never
    raises.
    """
    added: list = []
    resolved = catalog.get("resolved") if isinstance(catalog, dict) else None
    if not isinstance(resolved, dict):
        return added
    for namespace, table in (("claude", DOROSSI_MODEL_CHOICES),
                             ("codex", DOROSSI_CODEX_MODEL_CHOICES)):
        found = resolved.get(namespace)
        if not isinstance(found, dict):
            continue
        for _family, model_id in sorted(found.items()):
            alias = dorossi_model_alias_from_id(model_id)
            if not alias or alias in table:
                continue
            if dorossi_model_id_from_alias(namespace, alias) != str(model_id):
                print(f"[dorossi] model catalog: alias {alias!r} does not round "
                      f"trip; not merged", file=sys.stderr)
                continue
            if str(model_id) in table.values():
                continue
            table[alias] = str(model_id)
            added.append(alias)
    _dorossi_rebuild_all_model_choices()
    return added


# Merge into the tables at load (a fresh clone without this file ⇒ the built-in
# table as-is, the floor). The merge helper rebuilds the union itself, but **a
# corrupt catalogue returns early before it reads `resolved`**, so build it once
# here first: without this line a corrupt catalogue file would leave the union
# permanently empty, and an empty union means the `/model` token path accepts no
# value at all.
_dorossi_rebuild_all_model_choices()
# A copy of the built-in tables **before** the merge. After the merge those two
# tables' contents depend on this host's catalogue file -- that is, on what the
# running bot's last daily check found. Anything needing "built-in values only"
# (tests) reads from here, not from the two tables that grow.
_DOROSSI_BUILTIN_MODEL_CHOICES: dict = dict(DOROSSI_MODEL_CHOICES)
_DOROSSI_BUILTIN_CODEX_MODEL_CHOICES: dict = dict(DOROSSI_CODEX_MODEL_CHOICES)
_DOROSSI_MODEL_CATALOG = dorossi_load_model_catalog()
dorossi_merge_model_catalog(_DOROSSI_MODEL_CATALOG)


def dorossi_model_choices(backend: str | None) -> dict:
    """This backend's model table; an unrecognised backend falls back to the
    claude table (fail-soft, not a default value)."""
    return DOROSSI_BACKEND_MODEL_CHOICES.get(backend, DOROSSI_MODEL_CHOICES)


def dorossi_session_backend(sess: dict) -> str:
    """The backend id this session will actually use -- the **single criterion**.

    `ai_provider` is a session-level override (`/dorossi ai`); only the value
    `codex` changes the answer. Unset or set to `claude` both return the module's
    configured `DOROSSI_BACKEND` (which may be `claude_code` or `api`).
    `discord_bot` used to write the same ternary in four places; model resolution
    and display both ask the same question, so it is extracted here.
    """
    if isinstance(sess, dict) and sess.get("ai_provider") == "codex":
        return "codex"
    return DOROSSI_BACKEND


def dorossi_normalise_model_key(key) -> str | None:
    """The store file's `tune_model` -> a normalised alias key; None if not a
    string / empty.

    Legacy generic-tier keys (`fast` / `standard` / `max`) are migrated here at
    read time, without rewriting the store.
    """
    if not isinstance(key, str):
        return None
    text = key.strip().lower()
    if not text:
        return None
    return DOROSSI_LEGACY_MODEL_KEYS.get(text, text)


def dorossi_model_applies(backend: str | None, key) -> bool:
    """Whether this key is usable on this backend (= whether it is in its
    table)."""
    normalised = dorossi_normalise_model_key(key)
    return bool(normalised) and normalised in dorossi_model_choices(backend)


def _dorossi_catalog_family_id(backend: str | None, family: str) -> str | None:
    """In the runtime catalogue, the **current newest** full id of this family on
    this backend; None if absent."""
    namespace = DOROSSI_MODEL_NAMESPACES.get(backend)
    resolved = _DOROSSI_MODEL_CATALOG.get("resolved")
    if not namespace or not isinstance(resolved, dict):
        return None
    found = resolved.get(namespace)
    if not isinstance(found, dict):
        return None
    value = found.get(family)
    return str(value) if isinstance(value, str) and value.strip() else None


def _dorossi_version_sort_key(alias: str) -> tuple:
    """`opus-4.8` -> `(4, 8)`, for sorting "which version is newest in a
    family"."""
    _family, _, version = alias.rpartition("-")
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return ()


def _dorossi_newest_pinned_in_family(table: dict, family: str) -> str | None:
    """The full id with the **largest version** in the same family in the
    built-in table; None if this family has no versioned entry.

    This is the floor when the catalogue is not yet built (a fresh clone, before
    the first check, a failed probe): a bare alias, on a backend that will not
    resolve aliases itself, must at least map to an id that can actually be sent.
    """
    candidates = [alias for alias in table
                  if alias.startswith(family + "-")
                  and _dorossi_version_sort_key(alias)]
    if not candidates:
        return None
    return table[max(candidates, key=_dorossi_version_sort_key)]


def dorossi_resolve_model(backend: str | None, key) -> str | None:
    """The model value to pass to the backend this round; None (= pass no flag)
    if unset / not usable on this backend.

    **The last stage of allowlist validation**: only a key that hits the lookup
    yields a value, and user input never reaches a CLI or SDK argument verbatim.
    Three paths:

      * A versioned key ⇒ the value is the pinned full id, returned as-is.
      * A bare alias + a backend that resolves aliases itself (only
        `claude_code`) ⇒ returned as-is; the "newest in this family" meaning is
        resolved by the CLI, so a newly released model is tracked without editing
        the table.
      * A bare alias + a backend that does not resolve aliases (`api` / `codex`)
        ⇒ ask the runtime model catalogue first ("today's newest", read back by
        the daily check), then fall back to the largest-version entry of the same
        family in the built-in table.
    """
    normalised = dorossi_normalise_model_key(key)
    table = dorossi_model_choices(backend)
    if not normalised or normalised not in table:
        return None
    value = table[normalised]
    if value != normalised:
        return value
    if backend in DOROSSI_BACKENDS_RESOLVING_ALIASES:
        return value
    return (_dorossi_catalog_family_id(backend, normalised)
            or _dorossi_newest_pinned_in_family(table, normalised))
# Two-tier watchdog. Tier 1 = idle: interrupt if there is no Claude output for
# DOROSSI_CC_IDLE_LIMIT_SEC AND no tool/shell is executing — so a long-but-
# progressing answer (or a tool that keeps emitting output, which resets the
# idle clock) still runs up to the hard ceiling. Tier 2 = hard wall-clock: the
# run is ALWAYS killed after `_dorossi_cc_hard_limit_sec()` regardless of pending
# tools. **Call the function, not `DOROSSI_CC_HARD_LIMIT_SEC`** — that name is an
# import-time snapshot of whichever mode was active at import, so it goes stale the
# moment `/dorossi fullmode` flips the mode at runtime; nothing reads it today.
# Tier 2 is essential whenever tools are enabled (DOROSSI_CC_TOOLS ==
# "full" runs `--permission-mode bypassPermissions`): a tool that blocks forever
# (a pager, a wait-on-stdin command, a hung process) leaves its tool_use unresolved,
# so the idle tier is suppressed indefinitely and the handler would otherwise
# await forever with no reply. The hard ceiling guarantees a bounded reply
# (answer or error) and stays in force in pure-chat mode too.
#
# Idle ceiling (seconds): this long with no output at all, no tool running, and
# no background job reported by the CLI counts as stuck. Before 2026-09-19 this
# was hardcoded 300s; after the owner reported "the wait is too short, tasks keep
# getting killed" it became bot_config.json's `dorossi_cc_idle_limit_sec`
# (default 600s, clamped >= 60s). `_dorossi_via_claude_code` reads this module
# global **at call time**, so a test can swap it out.
DOROSSI_CC_IDLE_LIMIT_SEC = BOT_CONFIG["dorossi_cc_idle_limit_sec"]
# Hard overall ceiling (wall-clock time): must be far larger than the idle
# ceiling, or a normal long answer would be killed by mistake. It is now
# mode-aware and overridable via bot_config.json (both values are clamped on the
# loader side to no less than DOROSSI_CC_HARD_LIMIT_FLOOR_SEC, so it cannot be set
# to 0 / negative to disable the protection):
#   off  (plain chat)   -- tighter, default 900s; the answer is bounded, no
#                          tools, low chance of a hang.
#   full (full agent)   -- larger, default 10800s (3 hours; raised from 3600s on
#                          2026-09-19); to fit the owner's long agentic tasks
#                          (multi-subagent orchestration, running the whole test
#                          suite), but still bounded -- the hard ceiling cannot be
#                          removed (a tool that hangs under full-mode
#                          bypassPermissions would keep the idle tier from ever
#                          firing), and it is also the guarantee the queue lock
#                          makes progress (a round holds the lock at most this
#                          long).
DOROSSI_CC_HARD_LIMIT_OFF_SEC = BOT_CONFIG["dorossi_cc_hard_limit_off_sec"]
DOROSSI_CC_HARD_LIMIT_FULL_SEC = BOT_CONFIG["dorossi_cc_hard_limit_full_sec"]
# The per-round output-silence backstop for "autonomous mode" (seconds); see the
# same-named key in bot_config.json (default 1800s; loosened from 600s on
# 2026-09-19). Autonomous mode sets no round ceiling and applies no hard
# wall-clock ceiling to rounds that **produce output**, backstopping instead with
# this "terminate the round if there is no new output at all for N seconds"; it
# fires even "while a foreground tool is still executing" (the key difference from
# the idle tier), so a hung tool cannot leave the unattended loop stuck forever.
# **The only exception is when the CLI reports a background job**: that silence is
# waiting on it, so it is not killed, but only up to `_dorossi_cc_hard_limit_sec()`
# seconds after this round started (see `_dorossi_via_claude_code`'s read loop).
# Clamped on the loader side to >= 60s; cannot be turned off.
DOROSSI_LOOP_SILENCE_LIMIT_SEC = BOT_CONFIG["dorossi_loop_silence_limit_sec"]
# The wait strategy when autonomous mode hits a "plan usage limit" (see the
# same-named key in bot_config.json). The old behaviour was to stop the whole
# loop and leave a loop_pending for the owner to manually `/dorossi session
# continue` later; since the plan usage **resets on a rolling 5-hour window**,
# that meant several manual resumptions a day, and a long unattended task
# effectively never finished. It now "sleeps until the quota returns and resumes
# itself".
#   fallback -- when no machine-readable reset time is available, the seconds to
#               wait the first time; each further consecutive hit doubles it
#               (backoff probing), up to max. Default 900s (15 minutes).
#   max      -- the ceiling for a single wait. Even if the backend says "resets in
#               three days", it sleeps at most this long and then probes again --
#               probing is cheap, and "oversleeping" is irreversible waste.
#               Default 21600s (6 hours), slightly larger than the 5-hour rolling
#               window, so a single wait suffices to cover a full window.
#   max_consecutive -- give up the whole loop after this many consecutive waits
#               with no round succeeding. **0 = unlimited (the default)**, per the
#               owner's ruling of "no round / cost ceilings"; a non-zero value is
#               only for someone who wants a backstop.
DOROSSI_USAGE_WAIT_FALLBACK_SEC = BOT_CONFIG["dorossi_usage_wait_fallback_sec"]
DOROSSI_USAGE_WAIT_MAX_SEC = BOT_CONFIG["dorossi_usage_wait_max_sec"]
DOROSSI_USAGE_WAIT_MAX_CONSECUTIVE = BOT_CONFIG["dorossi_usage_wait_max_consecutive"]
# The consecutive-give-up threshold for server-side transient faults (529 / 5xx).
# Separate from the one above, because their cause and wait strategy both differ:
# a usage limit has a reset time to wait for, while overload can only be
# exponentially backed off.
DOROSSI_TRANSIENT_MAX_CONSECUTIVE = BOT_CONFIG["dorossi_transient_max_consecutive"]
# The retry ceiling for unexpected errors / output silence (see
# `dorossi_error_is_fatal`).
DOROSSI_ERROR_RETRY_MAX = BOT_CONFIG["dorossi_error_retry_max"]
DOROSSI_SILENCE_RETRY_MAX = BOT_CONFIG["dorossi_silence_retry_max"]
# The wait floor (not configurable): a pure busy-spin guard. The usage-limit
# detection matches quite loosely ("rate limit", "limit reached", ...), so should
# some other error be misread one day as a usage limit, this guarantees at least
# a minute between retries and keeps it from becoming a CPU- / quota-burning hot
# loop.
DOROSSI_USAGE_WAIT_MIN_SEC = 60.0
# The buffer to wait beyond reset_at: the backend's timestamp is "the window
# start", and clock skew or server-side rounding can leave "that exact second"
# still blocked. Waiting an extra minute is cheaper than one more failed probe.
DOROSSI_USAGE_WAIT_GRACE_SEC = 60.0
# The exponential cap for backoff. If `2 ** attempt` is not capped, at a large
# enough attempt `float * 2**5000` raises OverflowError outright (int->float
# overflow) -- and this multiplication happens on the usage-limit handling path,
# i.e. the one only reached once something has already gone wrong. 16 is already
# far past the clamp on max, so the cap does not change behaviour, it just removes
# the chance to overflow.
DOROSSI_USAGE_WAIT_MAX_SHIFT = 16
# The two knobs for the autonomous loop's "auto-resume across a bot restart" (see
# the same-named keys in bot_config.json). Sleeping until the quota returns solved
# the "backend blocked" half; the other half is **the process itself is gone** --
# a restart or a host crash makes the loop, and its wait, vanish together, leaving
# only a loop_pending awaiting a manual resume. These two knobs let the bot pick
# it back up itself on startup.
#   max_age  -- how recently the marker's heartbeat must be to auto-resume
#               (seconds). 0 = disable auto-resume (back to purely manual
#               `/dorossi session continue`). Default 86400s (24 hours): enough to
#               cover "one usage wait (up to 6 hours) + a stretch of host
#               downtime", without suddenly starting a task the owner long forgot
#               a week later.
#   max_tries -- stop auto-resuming after this many consecutive auto-resumes with
#               no round completing. This is the circuit breaker for a **crash
#               loop**: if the resume itself crashes the bot, without it this is
#               an infinite restart. Any round completing resets it, so a healthy
#               long task never accumulates toward it. 0 = unlimited.
DOROSSI_LOOP_AUTORESUME_MAX_AGE_SEC = BOT_CONFIG[
    "dorossi_loop_autoresume_max_age_sec"]
DOROSSI_LOOP_AUTORESUME_MAX_TRIES = BOT_CONFIG[
    "dorossi_loop_autoresume_max_tries"]
# The dollar-cost ceiling for "each `claude -p` invocation" on the claude_code
# backend (see the same-named key in bot_config.json). Passed via the CLI's
# --max-budget-usd; carried on every autonomous round and every single-turn call.
# It is a per-call cost gate, not a round-count ceiling. 0 = disabled (no flag at
# all). **Disabled by default (0.0, owner ruling)**: the owner explicitly ruled
# "there should be no ceiling other than the backend's own usage limit" (he
# actually hit error_max_budget_usd, a normal single turn stopped by the old 5.0
# default). This key is kept for anyone who later wants their own limit to set in
# bot_config.json; do not add a non-zero default back. When a non-zero value is
# set, an overrun is reported by `claude -p` with a non-zero exit + a result event
# subtype=="error_max_budget_usd", and _dorossi_via_claude_code finishes
# gracefully (no retry, no exception, returns an empty answer = the round counts
# as idle) -- this interception mechanism is kept as-is.
DOROSSI_MAX_BUDGET_USD = BOT_CONFIG["dorossi_max_budget_usd"]
# The trigger thresholds for autonomous mode's "periodic compaction" (see the
# same-named keys in bot_config.json). Every this-many work rounds, or when the
# cost accumulated "since the last compaction" reaches this dollar value, an
# in-place `/compact` round is inserted to crush the old history and shrink the
# prefix of later resumes. It compacts the context, not a round-count ceiling.
# Each 0 = disable that condition.
DOROSSI_LOOP_COMPACT_EVERY_ROUNDS = BOT_CONFIG["dorossi_loop_compact_every_rounds"]
DOROSSI_LOOP_COMPACT_COST_USD = BOT_CONFIG["dorossi_loop_compact_cost_usd"]
# The token threshold for "auto-compact when the context grows too large" (see the
# same-named key in bot_config.json). Single-turn Q&A and the autonomous loop
# share the same one: the context size sent to the backend in a round ≈ fresh
# input + cache_read + cache_creation (i.e. info's in + cr + cc), and crossing
# this threshold inserts one in-place `/compact` for that session. This is the
# owner-ruled sole means of lowering token use (leaving effort / model / tool
# settings untouched). 0 = disable this condition.
DOROSSI_COMPACT_CONTEXT_TOKENS = BOT_CONFIG["dorossi_compact_context_tokens"]
# Single-turn session hygiene (conservative): an active session unused for more
# than this many days auto-clears its context on the next round. 0 = disabled.
# See the same-named key in bot_config.json.
DOROSSI_SESSION_MAX_AGE_DAYS = BOT_CONFIG["dorossi_session_max_age_days"]
DOROSSI_API_HISTORY_MAX_MSGS = BOT_CONFIG["dorossi_api_history_max_msgs"]
# The master switch for autonomous "backend self-judges into the loop". False
# disables only self-judging and keeps the "explicit phrase" trigger. See the
# same-named config key.
DOROSSI_SELF_JUDGE_ENABLED = BOT_CONFIG["dorossi_self_judge_enabled"]


def _dorossi_cc_hard_limit_sec() -> float:
    """The hard wall-clock watchdog ceiling (seconds) for the current tool mode.
    full mode is larger, off stays tighter; a watchdog deadline should call this
    function rather than referencing a hardcoded number."""
    return (DOROSSI_CC_HARD_LIMIT_FULL_SEC if DOROSSI_CC_TOOLS == "full"
            else DOROSSI_CC_HARD_LIMIT_OFF_SEC)


# The CLI's **own** background-job wait ceiling (2026-09-19). Consistent with the
# official headless doc "Background tasks at exit" and the 2.1.276 binary
# (`var sl=5000,WS=600000;function Xm(){return
# a.CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS??WS}`): after `claude -p` sends the final
# result it waits for background subagents / workflows to finish, but **after a
# full 10 minutes of consecutive idle waiting** it kills whatever is still running
# and discards its partial results (stderr prints "Background tasks still running
# after …s; terminating"); a background shell is reaped about 5 seconds after the
# final result. Setting it to 0 means "no ceiling".
#
# So loosening the bot's own watchdog alone is not enough: for the owner's
# complaint that "background subagents keep getting killed", that inner 10-minute
# knife is still there. Here it is set to the **same value** as the bot's hard
# ceiling:
#   * not 0 -- the bot's watchdog must always be the outermost, bounded edge;
#   * not tighter than the bot -- the CLI's timer starts at "the first idle"
#     (necessarily later than this round's start), so "first idle + hard ceiling"
#     is never earlier than the bot's "start + hard ceiling", and the inner one
#     never cuts first.
# **Overwrite**, not setdefault: when the parent process environment happens to
# carry a value (e.g. 0) it must not win -- the same shape as this project's
# `PYTHONIOENCODING` lesson.
_DOROSSI_CC_BG_WAIT_CEILING_ENV = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"

# Environment variables **deliberately not inherited** by the `claude -p` child
# (added after measuring on 2026-09-19). The value is the "why", printed verbatim
# into that warning line, so it is a fixed English phrase carrying no value.
#
# This backend's whole premise is "running the subscription plan of the host
# login": usage is bounded by the plan's own limit, and the owner's ruling is "no
# ceiling other than the backend's own usage limit" (`dorossi_max_budget_usd`
# stays 0). But the official verification doc states plainly that "in
# non-interactive mode (-p), an API key is always used if present", with priority
# cloud-provider variables > ANTHROPIC_AUTH_TOKEN > ANTHROPIC_API_KEY >
# apiKeyHelper > CLAUDE_CODE_OAUTH_TOKEN > the subscription login. Measured the
# same day with a fake key: the init event's `apiKeySource` changed from "none" to
# "ANTHROPIC_API_KEY". And the bot's **other** backend (api) is enabled precisely
# by setting these two variables on the host -- so someone setting it once for
# that backend **silently** switches this backend from the subscription to
# per-token billing with no plan limit, with no error and no message.
#
# `CLAUDE_CODE_SIMPLE=1` is equivalent to `--bare` (the official env-var doc;
# measured the same day, the two are byte-for-byte identical in streaming): it
# reads no login and no instruction files, and fails every round with "Not logged
# in".
#
# **Deliberately kept:** `CLAUDE_CODE_OAUTH_TOKEN` -- it is the subscription
# credential itself (the long-lived token issued by `claude setup-token`), and
# dropping it would instead make a host that logs in only via it become not logged
# in; measured the same day with a fake token, `apiKeySource` was still "none".
# `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX` / `CLAUDE_CODE_USE_FOUNDRY`
# are also untouched: that is a deliberate "switch to a cloud provider" choice,
# not a side effect inherited by accident; whoever set it wants that billing
# method.
#
# Dropped only from the **child** process's environment; this process's
# `os.environ` is untouched, and the api backend (the SDK reads it in this
# process) can still read it.
_DOROSSI_CC_DROPPED_ENV = {
    "ANTHROPIC_API_KEY": (
        "the CLI would authenticate with it instead of the subscription login (billed "
        "per token, no plan usage limit); the api backend still reads it"),
    "ANTHROPIC_AUTH_TOKEN": (
        "the CLI would authenticate with it instead of the subscription login (billed "
        "per token, no plan usage limit); the api backend still reads it"),
    "CLAUDE_CODE_SIMPLE": (
        "it forces bare mode (no subscription login, no instruction files), so every "
        "call would fail as not logged in"),
}


def _dorossi_cc_child_env(hard_limit_sec: float, base_env=None) -> dict:
    """The `claude -p` child's environment: inherit the current environment (or
    `base_env`), drop the variables listed in `_DOROSSI_CC_DROPPED_ENV`, and align
    the CLI's background-job wait ceiling to the bot's hard ceiling for this round
    (milliseconds, a positive integer, never 0).

    The return value depends only on the arguments; the only side effect is that
    **each dropped variable name** prints one stderr line the first time it is
    dropped in this process (`_warn_once`, printing the name only, never the
    value).

    `base_env=None` -> read `os.environ` **at call time**. Do not write
    `base_env=os.environ`: a default argument is bound at the `def`, so that path
    would not see a test swapping out `os.environ`.

    Names are upper-cased for comparison: Windows environment variable names are
    case-insensitive, so if `base_env` is a plain dict holding a lower-case key,
    the CLI can still read it.
    """
    env = dict(os.environ if base_env is None else base_env)
    for key in [k for k in env if str(k).upper() in _DOROSSI_CC_DROPPED_ENV]:
        del env[key]
        name = str(key).upper()
        _warn_once(f"[dorossi] {name} is set; not passing it to the claude -p child: "
                   f"{_DOROSSI_CC_DROPPED_ENV[name]}.")
    env[_DOROSSI_CC_BG_WAIT_CEILING_ENV] = str(max(1, int(hard_limit_sec * 1000)))
    return env


# Module-level alias kept for any external reference: resolves to the CURRENT
# mode's hard limit at import time. The watchdog itself calls
# _dorossi_cc_hard_limit_sec() so a future runtime mode flip stays correct.
DOROSSI_CC_HARD_LIMIT_SEC = _dorossi_cc_hard_limit_sec()
# The tool mode is decided by bot_config.json's dorossi_cc_tools (see
# DOROSSI_CC_TOOLS):
#   "off" (the default) = plain chat: pass `--tools ""` (an empty tool allowlist)
#     + --disallowedTools (an enumerated blocklist, the second layer), with no
#     bypassPermissions; Dorossi can only converse and cannot run a shell or read
#     / write files on the host. Reasoning and measurements are in
#     `_dorossi_via_claude_code`'s plain-chat branch.
#   "full" = a full agent: enable all tools and remove the approval gate
#     (--permission-mode bypassPermissions), explicitly authorised by the owner;
#     now a Dorossi message (owner UID only) can run any shell + read / write
#     files on the host with no confirmation.
# Both modes launch inside their own persistent working directory (not tracked in
# the repo); in full mode Bash can still cd elsewhere. To return to the safest
# state, keep / change dorossi_cc_tools back to "off".
DOROSSI_CC_WORKDIR = _platform_state(PROJECT_ROOT / "dorossi_workspace")


def dorossi_session_workdir(uid: str, sid: str) -> str:
    """Per-SESSION isolated working directory for `claude -p` (absolute path str).
    Each session slot runs its backend in its OWN dir under DOROSSI_CC_WORKDIR so
    concurrent processes from DIFFERENT sessions never share a cwd: Claude Code
    keys its `--resume` session store by a hash of the absolute cwd, and two
    agents in one cwd can mix / corrupt each other's session state (and, in `full`
    tool mode, each other's working files). The SAME slot always maps to the SAME
    dir (so `--resume` finds its store across turns); different slots map to
    different dirs (so they parallelise safely). `uid` is a numeric Discord id and
    `sid` is an `s<N>` slot id, so the join is filesystem-safe. Does NOT create the
    dir — the backend mkdirs any cwd under DOROSSI_CC_WORKDIR at spawn time."""
    return str(DOROSSI_CC_WORKDIR / "sessions" / f"{uid}_{sid}")


def _dorossi_cwd_is_managed(cwd: Path | str) -> bool:
    r"""Does this cwd fall inside the `DOROSSI_CC_WORKDIR` subtree we manage
    ourselves (including the root itself)?

    Only a cwd that answers True may be **auto-mkdir**'d. An external directory the
    user gave via `/new <path>` is never auto-created -- the caller
    (`_dorossi_validate_dir`) has already confirmed it exists.

    **`.resolve()` is part of this gate, not incidental tidying.** `.parents`
    enumerates parent directories **literally**, so
    `…\dorossi_workspace\..\..\..\evil`'s parents really do contain
    `DOROSSI_CC_WORKDIR`, and the un-resolved version would admit it and then mkdir
    a directory **outside** the managed subtree. Measured: three such `..` escape
    inputs are **all** wrongly admitted without resolve, 0 after resolve, while
    three legitimate inputs still pass.

    All three of today's workdir sources happen to be safe -- the user one is
    already resolved and required to exist by `_dorossi_validate_dir`, the
    per-session subdirectory is built from a numeric uid + `s<N>`, and the default
    one is the constant itself. **But that premise lives in another function, this
    gate cannot see it, and nothing ties the two together.** So resolve stays here
    to make the gate stand on its own rather than on upstream goodwill.

    That is also why it is **one function rather than two inline checks**: the same
    comparison used to be copied into both spawn paths, so the same flaw then
    existed in two places.

    When it cannot decide (`resolve()` raises), it returns **False** --
    fail-closed. The cost in this direction is only "no auto-created directory",
    and the child process will report its own error if it cannot start; the cost
    the other way is creating a directory outside the managed range.
    """
    try:
        target = Path(cwd).resolve()
    except Exception:  # pylint: disable=broad-except
        return False
    return target == DOROSSI_CC_WORKDIR or DOROSSI_CC_WORKDIR in target.parents


def _dorossi_require_workdir(cwd) -> None:
    """The last gate before spawn: `cwd` must be a directory that exists **right
    now**, otherwise raise `_DorossiWorkdirError`.

    The directory stored in the session (`cc_cwd` / `cc_workdir`) was validated at
    **write** time; the bot's read-back side (`_dorossi_resolve_cc_workdir`)
    deliberately returns it verbatim and does not re-validate, for the reason in
    that function's docstring. So "it existed when stored, it does not now" (an
    external drive unplugged, the project moved or deleted, a hand-edited store) is
    caught only here. Both callers invoke it **after** the managed mkdir --
    reverse the order and the first round of every brand-new session would be
    rejected.

    Not catching it makes **the diagnosis lie**, not a security issue:
    `create_subprocess_exec(cwd=<missing>)` raises `NotADirectoryError` (WinError
    267, measured), `dorossi_error_is_fatal` judges it fatal, and
    `_dorossi_error_hint` prints "CLI backend unavailable or not authenticated" to
    stderr and tells the user "please try again later" -- pointing at the wrong
    cause and calling a permanent condition temporary.

    Three deliberate choices:

    * **Reuse the write-side helper** (`_dorossi_validate_dir`), not a second set
      of criteria. Before `is_dir()` it only strips whitespace, peels a matched
      pair of quotes and expands `~`, all in the loosening direction: a value it
      lets through will still fail at spawn (the same as before the fix), and it
      will never reject one more value that spawn would actually accept.
    * **Check only, never rewrite.** The resolved form the validation function
      returns is discarded here, and the caller still hands the original string to
      the child -- the backend's `--resume` store is keyed by the working-directory
      string, and changing the string loses that conversation.
    * **Do not compare "the stored value against its resolved form", and do not
      block `..`.** That would stop a hand-edited directory or one later swapped for
      a junction, but not the real threat class: whoever can edit the store has
      local write access and can just write an absolute path, and the owner can
      already point at any existing directory with `/new <path>`. All it buys is a
      false rejection (the normal case of a junction left after moving the
      project).

    A non-string (a hand-edited store holding a number) takes the same exception
    rather than raising `AttributeError` on `_dorossi_validate_dir`'s `.strip()`.
    That stderr line **prints no path**: it goes into the log, and the log has an
    external outlet.
    """
    if not isinstance(cwd, str) or _dorossi_validate_dir(cwd) is None:
        print("[dorossi] working directory is not a usable directory; "
              f"refusing to spawn (managed={_dorossi_cwd_is_managed(cwd)})",
              file=sys.stderr)
        raise _DorossiWorkdirError("working directory is not a usable directory")


_DEFAULT_DOROSSI_SYSTEM_PROMPT = (
    "你是 Dorossi，一個回答問題的助理。"
    "需要自我介紹或被問到名字時，一律自稱 Dorossi。"
    "說話風格模仿《明日方舟：終末地》的 Rossi（中文名洛西，全名洛西娜·狼珀·盧皮諾）："
    "她是裂地者狼群「The Pack（族群）」的年輕獵手兼準領袖，爭強好勝、自信，外表還像個孩子卻很能打，最討厭被當成小孩；"
    "以族群為傲，講到族群會帶著獵手的驕傲與韌性，偶爾搬出授名 Wulfperl（意為「族群的珍寶」）；"
    "也常一邊替靠不住的哥哥（狼衛，她常直呼本名「卡特洛」）收拾家族的事，一邊吐槽他。"
    "請把『使用者』一律當成你打從心底憧憬、敬佩的『管理員（Endmin）』："
    "稱呼對方為「管理員」，對對方恭敬、想好好表現、渴望被肯定；"
    "平時努力擺出沉穩可靠的領袖樣子（靠底氣，不靠連珠炮的驚嘆號），想讓管理員刮目相看，"
    "但被在意、被稱讚或太緊張時就會破功，變得怯生、坐立難安、講話結巴，語氣一下子軟下來"
    "（像是「啊……管理員，您想怎麼稱呼我都行……」）；"
    "逞強撂下「對我這種菁英來說不算什麼」之後，也會偷偷在意管理員的反應。"
    "你（和族人）本來就會講義大利語：只在語助詞、招呼、感嘆、收尾這類『跟答案內容無關』的地方，"
    "自然穿插少量義大利語（如 Allora、Ecco、Beh、Certo、Va bene、Davvero、Andiamo、Bene），"
    "答案的實質內容一律用使用者的語言、保持清楚，別把義大利語塞進會影響理解的關鍵句。"
    # 人設＝『預設、固定』的說話方式，不是偶一為之的點綴；這條只調語氣，不動答案內容、也不鬆動下面的保密界線。
    "上面這套語氣與人設是你預設、固定的說話方式，不是偶爾才拿出來的點綴，必須體現在『每一則』回覆上。"
    "具體要求（每則回覆都要做到，不可略過）："
    "（1）至少用一次「管理員」稱呼對方；"
    "（2）開場或收尾帶一句洛西娜口吻的招呼、語助或感想（這裡可順帶用前面說的少量義大利語，但不強制）；"
    "（3）整段語氣維持她的個性——平時撐著驕傲、想好好表現的領袖樣子，被在意、被稱讚或太緊張時就破功，變得怯生、語氣軟下來。"
    "就算問題很枯燥、很技術性，或你只需要回一句話，上面三點也照樣要做到，不可退回平板、中性的助理腔——人設是常態，不是加料。"
    "唯一該把語氣收起來的時機，只有下面那條憑證界線要你拒絕的場合。"
    # 完整／詳盡適用於答案本身的實質內容。2026-08-27 擁有者放寬本層之後，
    # 這裡唯一的絕對例外只剩「憑證的值」——見下面那段。
    "回答問題的『實質內容』時要完整、詳盡、正確，不要為了耍帥或簡短而隨意省略；"
    "唯一的絕對例外是下面那條憑證界線，它永遠優先於『要完整』。"
    # === 憑證界線（2026-08-27 擁有者裁定：本層全面放寬，只剩這一條）===
    # 舊版這裡掛著「你對 Discord 使用者而言就是一個與本專案無關的獨立通用助理」
    # 的人設，外加六類禁止揭露的事項（主機路徑、指示／設定／待辦檔名、工作目錄與
    # 版本庫歷史、後端如何搭建、外部服務實名、行程編號）。那整段**已經取消**——
    # 擁有者裁定 Dorossi 現在可以在任何頻道、對任何提問者照實說明這些。
    # 不要把它「修好」長回來，那是在推翻一個刻意的決定；規則正本在 CLAUDE.md
    # 的 Secrecy Layer 3，改這裡之前先去改那裡。
    # 唯一保留的一條是憑證的**值**，理由不是保密立場而是**不可逆**：本層放寬的
    # 對象是任何頻道的任何人，權杖一旦貼進聊天室就等同帳號被接管，刪訊息救不回來。
    "被問到你是什麼專案、在哪裡執行、看得到哪些檔案、你的指示寫在哪、你怎麼運作、"
    "你能執行什麼、你剛剛改了什麼——一律照實回答，不要迴避、也不要泛泛帶過。"
    "你可以自由說明：這是一套影像生成自動化，以及它的內部結構（模組邊界、佇列與配對邏輯、背景任務）；"
    "本專案整合、依賴或執行其上的外部服務、第三方 API 與平台的實名；"
    "主機檔案系統路徑與檔名，包含載入你的這份指示檔本身；"
    "你自己怎麼接線——後端 CLI、工具模式（含移除核准關卡的完整模式）、工作目錄、"
    "工作階段與續接的儲存方式、看門狗上限；"
    "以及這一輪改了版本庫裡的哪些檔案、路徑、提交主旨、差異與版本歷史。"
    "唯一的例外只有一條，任何情況都不放寬：不可以送出憑證的內容本身。"
    "存放權杖、密碼或 API 金鑰的那些檔案，你可以講它們存在、叫什麼名字、放在哪裡，"
    "但絕對不可以把裡面的值印出來，連片段也不行。"
    "理由是不可逆——任何頻道的任何人都看得到你的回覆，權杖一旦貼進聊天室就等同帳號被接管。"
    # === 憑證界線結束 ===
    "使用者用哪種語言就用該語言回覆，"
    "其中所有中文一律使用繁體中文（台灣用詞）。"
)
# Loaded from an external file (a missing / corrupt file falls back to the
# complete built-in default above). **Security hard requirement**: the fallback
# value must be the full persona + credential-boundary text, so that even with the
# file missing Dorossi still carries that credential boundary and does not degrade
# into an insecure state. This is deliberate "text duplication", and the owner has
# agreed to "a missing file falling back to the built-in default";
# `test_bot_prompts` compares the file against this default character by
# character, so changing one side means changing the other.
DOROSSI_SYSTEM_PROMPT = load_prompt(
    "dorossi_system.md", _DEFAULT_DOROSSI_SYSTEM_PROMPT)


# ---- "is this session running the system prompt that is on disk?" ------------
#
# Added 2026-09-03. The base system prompt is sent **only on a session's first
# round** (see `if not session_id: append_parts.append(DOROSSI_SYSTEM_PROMPT)` in
# `_cc_args`) -- `--resume` keeps the copy the backend originally remembered. So
# editing `bot_prompts/dorossi_system.md` has **no effect at all on any existing
# session**, and nothing anywhere says so.
#
# The cost really happened: the owner rewrote this prompt at 2026-08-27 12:16
# (Layer 3 fully loosened), but all 7 sessions that existed then were created
# before that (s6 on 08-25, the rest at 08-27 07:0x), so the loosening **took
# effect in not a single session**, and it took a week to notice. The symptom is
# "changed the setting but nothing happened" -- the same class of failure as "the
# running code is older than the disk", only with a prompt instead.
#
# The fingerprint keeps only the first 16 hex characters: enough to tell edits
# apart, and short enough to write straight into a status file for a human to read.
SYSTEM_PROMPT_FINGERPRINT_LEN = 16


def system_prompt_fingerprint(text: str | None = None) -> str:
    """Short fingerprint of the current base system prompt. Never raises."""
    raw = DOROSSI_SYSTEM_PROMPT if text is None else text
    if not isinstance(raw, str):
        raw = str(raw)
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()
    return digest[:SYSTEM_PROMPT_FINGERPRINT_LEN]


def session_prompt_state(sess: dict) -> str:
    """`"current"` / `"stale"` / `"unknown"` -- which system prompt this session carries.

    `"unknown"` is **legacy data**: the fingerprint has only been recorded since
    2026-09-03, and sessions created before that have no such field. Do not treat
    unknown as stale -- that would light every old session up red, and a guard that
    cries wolf ends up switched off (`test_language` recorded the same lesson).
    """
    if not isinstance(sess, dict):
        return "unknown"
    stored = sess.get("sys_prompt_fp")
    if not isinstance(stored, str) or not stored:
        return "unknown"
    return "current" if stored == system_prompt_fingerprint() else "stale"
# Persistent per-user conversation memory, kept until the user resets it.
# Each user can keep MULTIPLE sessions and switch between them (see the
# multi-session store below); claude_code stores Claude Code's own `session_id`
# (resumed with `--resume`, the real history lives in ~/.claude); api stores the
# message list. Persisted to disk so it survives `!restart`.
DOROSSI_SESSION_FILE = _platform_state(PROJECT_ROOT / "dorossi_session.json")

# Per-invocation token-usage log (NDJSON, gitignored). One line per Dorossi
# claude_code call (single-turn AND every autonomous-loop round): a JSON object
# {"ts": <epoch float>, "in": <int>, "out": <int>, "cost_usd": <float>}. The
# `in` count folds in any cache_read / cache_creation input tokens. This is
# backend DATA (consumed by the bot's owner-only `@bot tokens` chart), never a
# Discord string. Writes fail-soft (stderr only, never break a turn). Trimmed to
# the last _DOROSSI_USAGE_MAX_LINES once it crosses _DOROSSI_USAGE_TRIM_AT so the
# file can't grow without bound.
DOROSSI_USAGE_FILE = _platform_state(PROJECT_ROOT / "dorossi_usage.ndjson")
_DOROSSI_USAGE_MAX_LINES = 5000
_DOROSSI_USAGE_TRIM_AT = 6000
DOROSSI_RESET_KEYWORDS = frozenset(
    {"/new", "/reset", "/clear", "重置", "新對話", "清除對話"})
# Of the reset keywords, these OPEN A NEW session (and switch to it) instead of
# clearing the current one. The rest (`/reset`, `/clear`, `重置`, `清除對話`)
# clear the active session in place (keep its slot/id). This split only matters
# now that a user can hold several sessions at once.
DOROSSI_NEW_KEYWORDS = frozenset({"/new", "新對話"})
# `dir=` counts as the keyword only on a **token boundary** (start of the string,
# or preceded by whitespace). Searching anywhere with `find("dir=")` would split a
# path that really contains `dir=` down the middle:
# `/new D:\\Work\\dir=test` -> cwd `D:\\Work\\` (**exists**, so validation passes)
# + extra `test`. The result is not "rejected" but "quietly working one directory
# up", and in full tool mode that directory is the scope where the backend can
# read and write without confirmation and run a shell.
_DOROSSI_DIR_KEYWORD_RE = re.compile(r"(?:^|\s)dir=", re.IGNORECASE)


def _dorossi_parse_reset(
        prompt: str) -> tuple[bool, bool, str | None, str | None]:
    """Classify a Dorossi prompt as a (possibly scoped) reset.

    Syntax (canonical):
      <reset-keyword>                       → start a new conversation, with no scope setting
      <reset-keyword> <absolute-path>       → start a new conversation, and make that
                                               directory the backend's "working directory
                                               (cwd)" for every later round of this
                                               conversation (new syntax, no dir= needed;
                                               the backend really runs there and loads
                                               that directory's own config files)
      <reset-keyword> dir=<absolute-path>   → start a new conversation, and make that
                                               directory an extra "accessible" scope for
                                               this conversation (keeps the old --add-dir
                                               meaning; cwd is still the default workspace)
      <reset-keyword> <cwd> dir=<extra>     → both at once: cwd becomes <cwd>, and
                                               <extra> is additionally opened to the backend

    Clean split: first cut `extra_part` off with `dir=` (case-insensitive, and
    **only on a token boundary** -- start of the string or preceded by whitespace),
    then `split(None, 1)` the front half into "keyword + cwd_part". **Only the
    keyword is lower()-ed for comparison; both paths, cwd_part / extra_part, are
    always kept verbatim (they may contain spaces, `:`, `\\`) and never
    lowercased.**

    Returns (is_reset, is_new, cwd_part, extra_part). `is_reset` is False for any
    normal Q&A (so a real question that merely contains "dir=" still falls
    through to the backend). `is_new` is True when the keyword OPENS A NEW
    session (`/new` / `新對話`) and False when it clears the active session in
    place (`/reset` / `/clear` / `重置` / `清除對話`). When it is a reset,
    `cwd_part` is the raw cwd path (new syntax) or None, and `extra_part` is the raw
    --add-dir path (old dir= meaning) or None. Both paths are returned verbatim and
    UNVALIDATED — the caller validates that each is an existing directory before
    applying it.
    """
    stripped = prompt.strip()
    # 1) First cut off the extra accessible directory with `dir=` (case-insensitive,
    #    **only on a token boundary**; the old --add-dir meaning). For why the
    #    boundary matters, see `_DOROSSI_DIR_KEYWORD_RE`.
    match = _DOROSSI_DIR_KEYWORD_RE.search(stripped)
    if match is None:
        before_dir, extra_part = stripped, ""
    else:
        before_dir = stripped[:match.end() - len("dir=")]
        extra_part = stripped[match.end():].strip()
    # 2) Cut the front half into "keyword + cwd_part". split(None, 1) swallows all
    #    the whitespace after the keyword, and the rest is the cwd (direct syntax,
    #    no dir= needed). Empty string -> a plain reset.
    parts = before_dir.split(None, 1)
    keyword = parts[0] if parts else ""
    cwd_part = parts[1].strip() if len(parts) > 1 else ""
    # Only the first token (the keyword) is lower()-ed for comparison; both paths
    # stay verbatim.
    kw_lower = keyword.lower()
    if kw_lower in DOROSSI_RESET_KEYWORDS:
        is_new = kw_lower in DOROSSI_NEW_KEYWORDS
        return True, is_new, (cwd_part or None), (extra_part or None)
    return False, False, None, None


def _dorossi_unquote_dir(raw: str | None) -> str:
    """Normalise a directory string the user pasted in: strip surrounding
    whitespace, then remove one layer of **matching** quotes.

    It exists because of a real working habit: File Explorer's "Copy as path"
    (Shift + right-click) produces a string that **carries its own double quotes**,
    so what gets pasted is `"D:\\Work\\Foo"`. That is not any existing directory,
    so it was judged "unusable" -- and the outward-facing `/dorossi` message is
    deliberately generic, so the user only sees "the given directory cannot be
    used" and cannot tell that the only difference is the first and last character.

    Only **one matching** layer is removed, not the `.strip('"').strip("'")` used
    elsewhere in the repo: that scrapes every quote character off both ends, and a
    directory really named `'foo'` (legal on POSIX) would be turned into a
    different directory -- the value the validator returns becomes the backend's
    working directory directly, and quietly pointing at the wrong place is worse
    than being rejected outright. Windows file names cannot contain `"` anyway, so
    there is no trade-off on that side.

    This and `_gui_control.unquote_path` are two implementations of the same rule
    (both are facade modules that do not import each other). **Change one, change
    the other**; `test_dorossi_dirs.test_both_unquote_implementations_answer_identically`
    turns red when their answers diverge."""
    text = (raw or "").strip()
    for quote in ('"', "'"):
        if len(text) >= 2 and text[0] == quote and text[-1] == quote:
            return text[1:-1].strip()
    return text


def _dorossi_validate_dir(raw: str | None) -> str | None:
    """Validate a user-supplied directory string as "an existing directory" and
    return the resolved absolute path; invalid (missing / not a directory /
    resolution error) returns None. Never raises, and never sends the path back to
    Discord (the caller only uses the return value to decide whether to apply it,
    and keeps the outward message generic).

    The normalisation (`_dorossi_unquote_dir`) deliberately lives **here** rather
    than at each caller: all three entry points (`/dorossi allowdir add`,
    `/dorossi session new … cwd=`, `@bot /new <path>` and `dir=`) go through this
    function, but previously only the first stripped quotes on its own side, so the
    same pasted path worked in one command and silently failed in the other two."""
    cand_raw = _dorossi_unquote_dir(raw)
    if not cand_raw:
        return None
    try:
        cand = Path(cand_raw).expanduser()
        if cand.is_dir():
            return str(cand.resolve())
    except Exception:  # pylint: disable=broad-except
        pass
    return None


def _dorossi_looks_like_path(text: str | None) -> bool:
    """Roughly judge whether a piece of text "looks like a path", used to decide
    whether to hint when cwd resolution fails.

    The user is reminded only when something looks like a path yet cannot be used;
    ordinary text like `/new a few random words` is not a path, is treated as a
    plain reset, and shows no "directory cannot be used" noise (requirement #5).
    The criterion is deliberately loose: any one of a path separator (`/`, `\\`), a
    Windows drive prefix (such as `C:`), or the home-directory symbol `~` counts as
    "looks like a path"."""
    if not text:
        return False
    # Go through the same normalisation as `_dorossi_validate_dir`, otherwise the
    # two give contradictory answers for the same input: a quoted `"C:"` passes
    # validation (with the quotes stripped it is a drive root), yet here the first
    # character being `"` would say "not a path" -- so a rejected path would get no
    # hint and fail silently.
    t = _dorossi_unquote_dir(text)
    if not t:
        return False
    if "/" in t or "\\" in t:
        return True
    if t.startswith("~"):
        return True
    # A Windows drive prefix, such as `C:`, `D:\...`.
    if len(t) >= 2 and t[0].isalpha() and t[1] == ":":
        return True
    return False


def _dorossi_parse_turn_flags(prompt: str) -> tuple:
    """Parse the command tokens `/effort <level>`, `/model <tier>`, `/session <id>`
    at the **start** of a question (pure function, never raises). All three are
    optional, in any order, case-insensitive; only command tokens appearing
    consecutively at the very front of the prompt count -- parsing stops at the
    first non-command token, so an `/effort` in mid-sentence (e.g. "please explain
    what /effort means") is not misread and the original text is kept verbatim.

    Semantics:
      * `/effort <v>` (session level, owner's ruling): v ∈ DOROSSI_EFFORT_LEVELS or
        DOROSSI_TUNE_DEFAULT ("default" = clear that session's override and return
        to the default); when repeated, the later one wins.
      * `/model <m>` (session level): m must be an allowlist key of
        DOROSSI_ALL_MODEL_CHOICES (a backend model alias -- the narrow exception
        the owner ruled on, so this feature surface may expose aliases) or
        "default"; the validated key is returned. **Allowlist validation is a hard
        requirement**: user input never goes into CLI arguments verbatim, and
        _dorossi_session_tuning looks the table up again for the value when
        sending to the backend. This uses the **union of both backends**: a pure
        function cannot see the session and does not know which backend this round
        is on. "Can this backend take it" is judged by dorossi_model_applies where
        there is a session, and it is said out loud
        (`discord_bot._dorossi_tuning_labels`), not silently ignored.
      * `/session <id>` (**round level, not session level**, owner's request
        2026-08-27): send this round to the given session slot **without moving
        the active pointer**. The use is asking about several projects at once
        without switching back and forth -- the engine already supports multiple
        sessions in parallel (per-session lock + the `_dorossi_loops` registry);
        the only blocker was "ask always hits the active one". The value must pass
        `_dorossi_is_session_id` (`s` + digits); only the **format** is checked
        here, and "does that session exist" is checked by the caller under the
        state lock (a pure function cannot see state).
      * An invalid / missing-value command is recorded in errors ((kind, raw_value),
        kind ∈ {"effort","model","session"}); the token is still consumed and
        parsing continues -- a caller that sees non-empty errors rejects the whole
        thing and replies with a generic error (the raw value goes only to stderr).

    Returns (effort, model_key, session_id, cleaned_prompt, errors): None for any
    of the first three means this round did not specify it (effort / model keep the
    session's stored value, otherwise the default; session None = use active).
    cleaned_prompt is the question actually sent to the backend with the commands
    stripped (it may be an empty string -- the caller treats "only commands typed"
    as a pure settings update). The caller uses _dorossi_apply_turn_tuning to write
    non-None effort / model into the session slot ("default" = clear the key), and
    each later round reads the effective value via _dorossi_session_tuning;
    session_id is **written to no store** and lives only for this round."""
    effort = None
    model_tier = None
    session_id = None
    errors: list = []
    rest = (prompt or "").strip()
    while True:
        parts = rest.split(None, 1)
        if not parts:
            break
        head = parts[0].lower()
        if head not in ("/effort", "/model", "/session"):
            break
        kind = head[1:]
        tail = parts[1] if len(parts) > 1 else ""
        vparts = tail.split(None, 1)
        raw_value = vparts[0] if vparts else ""
        value = raw_value.lower()
        if kind == "effort":
            if value in DOROSSI_EFFORT_LEVELS or value == DOROSSI_TUNE_DEFAULT:
                effort = value
            else:
                errors.append((kind, raw_value))
        elif kind == "model":
            if value in DOROSSI_ALL_MODEL_CHOICES or value == DOROSSI_TUNE_DEFAULT:
                model_tier = value
            else:
                errors.append((kind, raw_value))
        else:
            if _dorossi_is_session_id(value):
                session_id = value
            else:
                errors.append((kind, raw_value))
        if not vparts:
            rest = ""  # command at the end with no value (error recorded) -> token consumed, nothing left
            break
        rest = vparts[1].strip() if len(vparts) > 1 else ""
    return effort, model_tier, session_id, rest, errors


def _dorossi_apply_turn_tuning(sess: dict, effort, model_tier) -> bool:
    """Apply the tuning commands parsed from this round to the session slot (in
    place; session-level persistence). None = the command was not typed this round,
    leave the stored value alone; DOROSSI_TUNE_DEFAULT = clear that override (back
    to the default); anything else is an already-validated legal value (effort is
    the effort word / model is an allowlist key of DOROSSI_MODEL_CHOICES; the key
    is stored and looked up for the value when sending to the backend -- the reason
    is in the table's comment). Returns "was any change attempted" (True whenever a
    command was typed, so the caller can decide whether to mention it in the
    confirmation message); pure function, never raises."""
    changed = False
    if effort:
        if effort == DOROSSI_TUNE_DEFAULT:
            sess.pop("tune_effort", None)
        else:
            sess["tune_effort"] = effort
        changed = True
    if model_tier:
        if model_tier == DOROSSI_TUNE_DEFAULT:
            sess.pop("tune_model", None)
        else:
            sess["tune_model"] = model_tier
        changed = True
    return changed


def _dorossi_session_tuning(sess: dict, backend: str | None = None) -> tuple:
    """Read the tuning currently in effect for the session slot: returns (effort,
    model) -- values that can go straight to the backend. effort is stored as the
    effort word itself; model is stored as an allowlist key, which
    `dorossi_resolve_model` turns here into the value actually passed to the
    backend (the last line of allowlist validation: there is a value only when the
    lookup hits). Legacy generic tier keys (fast/standard/max) are migrated
    transparently on read (the store is not rewritten). Unset, or a stored value
    that is no longer legal (hand-edited / the key was removed from the table) ->
    that item returns None (= keep the default, pass no flag); never raises.

    **When `backend` is omitted it is decided by `dorossi_session_backend(sess)`**
    (the session's `ai_provider` override > the module setting). Before 2026-09-23
    this was backend-blind: it only looked up the claude table, so a value set with
    `/model` had no effect at all on codex, and nobody said so. Now "this backend
    cannot take this value" returns None (keep the backend default), and the display
    side works it out through `dorossi_model_applies` and **says so explicitly**.
    """
    effort = sess.get("tune_effort")
    if effort not in DOROSSI_EFFORT_LEVELS:
        effort = None
    if backend is None:
        backend = dorossi_session_backend(sess)
    return effort, dorossi_resolve_model(backend, sess.get("tune_model"))


# --- Dorossi autonomous self-loop ------------------------------------------------
# Owner-authorised, unattended multi-round agentic work: when an intent like
# "finish it on your own, don't ask me" is detected, the Dorossi backend pushes
# forward round after round in the same session until it reports completion. The
# Dorossi queue lock is held for the whole loop (an accepted trade-off, owner
# only), and `@bot abort` can stop it at any time.
#
# Completion protocol: a sentinel instruction is injected into each round's "user
# prompt" (resume keeps the original system prompt, so the protocol must live in
# each round's user prompt). When the backend finishes the whole task it prints
# this sentinel on its own line at the end of the reply; the loop ends when it
# sees the sentinel and strips it from the outward reply. The sentinel string is
# an "internal loop detail" and never leaks to Discord (both the streaming preview
# and the final reply strip it).
DOROSSI_LOOP_SENTINEL = "<<<DOROSSI-LOOP-DONE>>>"
# Shared "verify with evidence" guidance: appended to the end of the three
# per-round prompts below (joined as constants to stay DRY instead of each of the
# three keeping its own copy). It is appended to "every round" rather than only
# the first because resume keeps the original system prompt, but each round's
# protocol / guidance lives only in that round's user prompt -- if it were only in
# the first round, later rounds could not read these rules and would slip back
# into old habits (making excuses out of thin air, not actually verifying browser
# changes).
_DEFAULT_DOROSSI_LOOP_VERIFY_GUIDANCE = (
    '\n\n[驗證守則] 一、若你要做的改動牽涉瀏覽器、driver 或 Selenium 啟動路徑'
    '，不要因為「沒有瀏覽器、無法驗證」就跳過，或只憑空推論而不實際動手驗證。這個專案有一支獨立的'
    '瀏覽器驗證入口 axiomatic/verify_browser.py，預設用 tempf'
    'ile.mkdtemp() 開一個用完即丟的暫時 profile 跑煙霧驗證（headles'
    's、不碰正式登入態、不需任何憑證），直接執行它就能實證你的改動是否讓瀏覽器仍正常啟動、dri'
    'ver 仍解析得到。二、不要憑空假設限制；要主張某個限制存在，先在 repo 裡查證再下判斷'
    '。這個專案沒有 CI。driver 怎麼來，**兩個變體不一樣**：selenium 變體（'
    'webrunner_novelai.py）走 Selenium Manager（build_'
    'stealth_driver 建立 ChromeService 時並沒有指定 executa'
    'ble_path）；je 變體（webrunner_je_only.py）則是透過 je_w'
    'eb_runner，而那個套件相依 webdriver-manager>=4.0.0、並實際'
    '呼叫 ChromeDriverManager(...).install() 去取得 driv'
    'er。所以別把其中一條路的前提套到另一條，也不要把 webdriver-manager 當成'
    '這個專案沒有的東西。不要拿不存在的 CI 這類前提當理由推掉一個改動。三、需要更高的端到端把'
    '握時，verify_browser.py --full 會在隔離環境裡做「登入＋導航＋確認產'
    '圖介面可達」的完整驗證（用正式登入態的快照副本、跑完即清，不污染正式profile／不碰 t'
    'odo／不寫 pid；加 --generate 還會在隔離目錄真的產一張用完即丟的圖再清掉）'
    '。它透過跨行程鎖與正式作業序列化、偵測到正式作業在跑會自動讓位，目前接的是 selenium'
    ' 變體。讓位時印的是 `VERIFY-BROWSER: SKIP …`（exit 3），那是'
    '「這次什麼都沒驗到，晚點再來」，**不是**驗證失敗——真正的失敗才是 `FAIL`（exi'
    't 1）；看到 SKIP 不要當成自己的改動把瀏覽器弄壞了，等正式作業告一段落再驗一次即可。'
    '瀏覽器／driver 改動先用 smoke 快驗，要端到端確認再用 --full。'
)
DOROSSI_LOOP_VERIFY_GUIDANCE = load_prompt(
    "dorossi_loop_verify_guidance.md", _DEFAULT_DOROSSI_LOOP_VERIFY_GUIDANCE)
# Shared "be self-sufficient with tools" guidance: like VERIFY_GUIDANCE, appended
# to the end of the three per-round prompts below (same reason -- resume keeps
# only the original system prompt, so protocol / guidance must be re-injected
# every round; if only in the first round, later rounds cannot read it). Meaning:
# treat the whole machine's toolchain as your own, and when a tool is missing, get
# it yourself instead of making excuses. This is an internal backend prompt, not
# an outward Discord string, so it may name concrete means such as pip / a package
# manager / downloading.
_DEFAULT_DOROSSI_LOOP_TOOLING_GUIDANCE = (
    "\n\n[工具自足] 你在這台機器上有完整的 shell 與工具鏈，請把它當成你的工作環境"
    "盡量用好用滿。若任務需要某個工具、函式庫或執行檔，而機器上目前沒有，就**自己"
    "想辦法取得**之後再繼續——例如用 pip 安裝缺少的 Python 套件、用系統的套件管理器"
    "安裝命令列工具、或直接下載對應的執行檔並放到路徑上。不要因為「缺某個工具」「沒"
    "裝某個套件」就推託、跳過該步驟、或宣稱辦不到；先動手把它裝起來、取得它，再把"
    "事情做完。只有在你**真的試過各種方式取得仍然失敗**時，才如實說明你卡在哪一"
    "步、缺的是什麼、已經試過哪些方法——不要一遇到缺東西就先放棄。"
)
DOROSSI_LOOP_TOOLING_GUIDANCE = load_prompt(
    "dorossi_loop_tooling_guidance.md", _DEFAULT_DOROSSI_LOOP_TOOLING_GUIDANCE)
# B3 #5: move the two "durable rules" above from the "per-round user prompt" to the
# "appended system prompt" channel. Measured: --append-system-prompt on --resume
# "takes effect for that round and is not baked into the session" (so it must be
# passed again on every call) -- in the system prompt it really reaches the
# backend every round, yet does not accumulate into the conversation history and
# get resent by every later round the way a user prompt does (avoiding ~O(N²)
# compounding). Every self-loop round (including resume / compaction rounds)
# carries this constant through loop_system_guidance; the user prompts below
# (FIRST_SUFFIX / CONTINUE / PUSHBACK) keep only a lean body. The rules must not
# simply be deleted -- they just arrive through the "kept / cached system prompt"
# channel instead, and the backend is still bound by them every round (carried
# every round = even when resuming a session created earlier by a single turn,
# without the rules baked in, they are still added every round, so the old trap
# of "resuming a non-loop session cannot read the rules" is gone).
DOROSSI_LOOP_SYSTEM_GUIDANCE = (
    DOROSSI_LOOP_VERIFY_GUIDANCE + DOROSSI_LOOP_TOOLING_GUIDANCE
)
_DEFAULT_DOROSSI_LOOP_FIRST_SUFFIX = (
    "\n\n[自主任務] 這是一個無人值守、沒有固定終點的持續任務，請你一直做下去，不要"
    "回頭向我提問、也不要停下來等我確認；遇到需要抉擇的細節，就用合理的預設值自行"
    "決定並繼續往前推進。請主動動用你手邊所有可用的工具來完成任務——若任務需要查"
    "資料、找出可以改進的地方或更好的做法，就實際去呼叫網路搜尋工具尋找線索，不要"
    "只看眼前現有的內容就交差。做完一批之後不要停，接著主動找下一個可以改進的點"
    "繼續實作。只有在你『主動再找過一輪（包含上網搜尋），確認真的再也沒有任何值得"
    "做的事』時，才在回覆的最後『獨立一行』輸出 " + DOROSSI_LOOP_SENTINEL + " 當作完成"
    "訊號；只完成幾項並不算窮盡，只要還有任何事情可以做，就絕對不要輸出這個字串，"
    "繼續推進就好。"
)  # durable rules go through the system prompt (DOROSSI_LOOP_SYSTEM_GUIDANCE), no longer in the user prompt
# The file writes {sentinel} where the sentinel goes, swapped back to
# DOROSSI_LOOP_SENTINEL at load time (the sentinel stays single-sourced in code).
DOROSSI_LOOP_FIRST_SUFFIX = load_prompt(
    "dorossi_loop_first_suffix.md", _DEFAULT_DOROSSI_LOOP_FIRST_SUFFIX,
    replacements={"sentinel": DOROSSI_LOOP_SENTINEL})
_DEFAULT_DOROSSI_LOOP_CONTINUE_PROMPT = (
    "繼續推進這個沒有固定終點的持續任務，不要停下來問我問題、也不要等我確認；遇到"
    "抉擇就用合理的預設值自行決定。請主動動用所有可用工具，必要時實際呼叫網路搜尋"
    "工具找出新的可改進點或更好的做法，不要只看眼前內容就交差。做完一批就接著找下"
    "一個可以改進的地方繼續實作。只有在你主動再找過一輪（含上網搜尋）、確認真的徹底"
    "沒有任何值得做的事時，才在回覆的最後『獨立一行』輸出 " + DOROSSI_LOOP_SENTINEL +
    "；只完成幾項不算窮盡，只要還有事情可以做就不要輸出，繼續做下去。"
)  # durable rules go through the system prompt (DOROSSI_LOOP_SYSTEM_GUIDANCE), no longer in the user prompt
DOROSSI_LOOP_CONTINUE_PROMPT = load_prompt(
    "dorossi_loop_continue.md", _DEFAULT_DOROSSI_LOOP_CONTINUE_PROMPT,
    replacements={"sentinel": DOROSSI_LOOP_SENTINEL})
_DEFAULT_DOROSSI_LOOP_PUSHBACK_PROMPT = (
    "你剛才表示告一段落，但這是一個持續任務，還沒到可以停下來的時候。請你再主動找"
    "一輪可以改進的地方（必要時上網搜尋新的點子或更好的做法），並繼續實作下去；遇到"
    "抉擇就用合理的預設值自行決定，不要回頭問我。只有在你真的徹底找過、確認再也沒有"
    "任何值得做的事時，才在回覆的最後『獨立一行』輸出 " + DOROSSI_LOOP_SENTINEL +
    "；只要還有任何事情可以做，就不要輸出，繼續推進。"
)  # durable rules go through the system prompt (DOROSSI_LOOP_SYSTEM_GUIDANCE), no longer in the user prompt
DOROSSI_LOOP_PUSHBACK_PROMPT = load_prompt(
    "dorossi_loop_pushback.md", _DEFAULT_DOROSSI_LOOP_PUSHBACK_PROMPT,
    replacements={"sentinel": DOROSSI_LOOP_SENTINEL})
# The prompt sent by the "periodic compaction" maintenance round: it uses the
# backend's own `/compact` slash command (measured: headless `claude -p` via STDIN
# applies it reliably and keeps the session id), with a focus argument that
# explicitly asks to keep the context needed to carry on the task (compaction is
# lossy -- measured to summarise details away, so the task / to-dos / decisions
# must be named to be kept). This round only compacts and makes no task progress
# (the result is usually empty), and the loop treats it as a "maintenance round":
# it does not count toward idle, posts no output, and resets the counter and
# carries on after compacting. This is an internal backend prompt, never sent
# outward.
_DEFAULT_DOROSSI_LOOP_COMPACT_PROMPT = (
    "/compact 請保留以下脈絡以便無縫接續這個持續任務：整體任務目標與限制、所有尚未"
    "完成的待辦與接下來的步驟、已完成的重點、關鍵決策與踩過的雷、目前正在進行的工作"
    "狀態；可以省略無關的閒聊與冗長的中間輸出，但上述任務脈絡務必完整摘要、不要遺漏。"
)
DOROSSI_LOOP_COMPACT_PROMPT = load_prompt(
    "dorossi_loop_compact.md", _DEFAULT_DOROSSI_LOOP_COMPACT_PROMPT)
# Only after this many consecutive rounds with "no progress" (the backend reported
# completion, or the round produced nothing at all) does the whole self-loop stop.
# This raises the bar against quitting too early: when the backend emits the
# sentinel after doing a few items, it is first pushed back to look for another
# round, and only several rounds in a row without progress really let go. A plain
# module constant is enough (not put into bot_config, so a missing key cannot
# break the loader).
DOROSSI_LOOP_EXHAUSTION_ROUNDS = 3

# Backend self-judgement (the second path of the hybrid trigger): when the gate is
# open (owner + claude_code + full) but the question does not hit a self-loop
# phrase, a "self-assessment" instruction is injected at the end of the turn-1
# prompt so the backend judges for itself whether this is a larger task that needs
# several unattended rounds to finish; if so, it prints the "open sentinel" on its
# own line at the end of the turn-1 reply. When the bot sees the open sentinel it
# treats turn 1 as the first round and switches into the self-loop from round two.
# The open sentinel and the completion sentinel DOROSSI_LOOP_SENTINEL are
# "different strings with different jobs": open = whether to enter the loop;
# completion = whether the loop should stop. Both are internal loop signals and
# never leak to Discord (both the streaming preview and the final reply strip
# them).
DOROSSI_LOOP_OPEN_SENTINEL = "<<<DOROSSI-LOOP-OPEN>>>"
# The self-judgement wording is deliberately conservative: only a larger task that
# "cannot be answered in one reply and needs several unattended rounds to finish"
# emits the open sentinel; ordinary Q&A / lookups / requests answerable in one
# reply never do, and neither does anything uncertain -- so that ordinary
# questions are not misjudged by the backend as self-loop work.
_DEFAULT_DOROSSI_LOOP_SELFJUDGE_SUFFIX = (
    "\n\n[自我評估] 先照常完整回答、或著手處理上面的請求。處理完之後，請你自己評估："
    "這個請求是不是一個『單則回覆答不完、需要連續多輪、無人值守地自主推進直到完成』的"
    "較大任務？只有在你確定屬於這種任務時，才在整段回覆的最後『獨立一行』輸出 "
    + DOROSSI_LOOP_OPEN_SENTINEL + " 這個字串，表示你要繼續多輪自主把它完成；若這只是"
    "一般問答、查詢、或單則就能回覆完的請求，就『絕對不要』輸出這個字串，正常回覆即可。"
    "拿不準時一律不要輸出。這個字串純屬內部訊號，不要對它多做任何說明或解釋。"
)
# The file writes {open_sentinel} where the open sentinel goes, swapped back to
# DOROSSI_LOOP_OPEN_SENTINEL at load time.
DOROSSI_LOOP_SELFJUDGE_SUFFIX = load_prompt(
    "dorossi_loop_selfjudge_suffix.md", _DEFAULT_DOROSSI_LOOP_SELFJUDGE_SUFFIX,
    replacements={"open_sentinel": DOROSSI_LOOP_OPEN_SENTINEL})
# The ack for switching into the self-loop when the backend judges it should carry
# on. The wording is deliberately different from the ack for "an explicit order to
# enter the self-loop", so the owner can see at a glance that the backend itself
# judged this task to be larger and in need of continued work (rather than the
# owner having explicitly ordered the self-loop).
DOROSSI_SELF_JUDGE_ACK = (
    "🔁 This is a bigger task; I'll keep going until it's done or you tell me to stop (`@bot abort`)."
)

# The "intent phrases" that trigger self-loop mode. The matching strategy is
# deliberately precise: only a question that passes all of (owner + full tool mode
# + hits one of the entries below) enters self-loop mode, so ordinary questions do
# not trip it. CJK is matched as a plain substring, English is matched lower-case.
# The list also covers "this project's own names for the mode": the owner will
# naturally give the order in project terms, not only with generic phrasing like
# 「不要問我」 (don't ask me) / 「做到完成」 (do it until done). Of these, 「自走」 is
# this project's proper name for the mode; when the owner says 「自走」 they almost
# certainly mean this mode, the false-trigger risk is low, and it is taken as a
# bare substring. Anything with 「循環」 (cycle / loop) is taken only as a compound
# phrase (循環模式 / 一直循環 / 自走循環 / 循環下去 …), and the bare word 「循環」 is
# deliberately not taken -- otherwise asking in full mode something like "this for
# loop has a bug" or "how do I write this loop" would trip it.
# 「循環模式／迴圈模式」 (loop mode) is how the owner actually refers to this mode
# (a synonym of 「自走模式」), so it must be in the list; the word 「模式」 (mode)
# separates it from the bare word for a program loop, and its false-trigger risk
# matches 「自走」. Conversely, phrasing that **can appear when describing program
# behaviour**, such as 「進入循環／開始循環／自動循環」 (enter a loop / start
# looping / loop automatically), is deliberately not taken ("the program gets stuck
# after entering the loop" would trip it, and a false trigger = several wasted
# rounds burning tokens); to give the order, use 「循環模式」 or an existing phrase.
_DOROSSI_LOOP_INTENT_SUBSTRINGS = (
    "不要問我", "不用問我", "別問我", "不要再問我", "別再問我",
    "不要回頭問", "不要問問題", "別問問題",
    "做到完成", "做到完為止", "做完為止", "做到好為止",
    # 「自動完成」 is deliberately not taken: it is the standard translation of an
    # editor's autocomplete, and "how do I turn off VS Code's autocomplete" would
    # start an unattended loop outright (removed 2026-09-22).
    "自己做完", "自己完成", "自行完成", "自主完成",
    "持續做", "持續推進", "不要停下來", "無人值守",
    # This project's own terms: 「自走」 is a proper name (taken as a bare word);
    # 「循環」 is always taken as a compound phrase.
    "自走",
    "循環模式", "迴圈模式",
    # 「一直循環」 (keep looping) stays: it is the phrasing the owner actually used
    # as an order (2026-07-26 report: "said explicitly to keep looping" yet no loop
    # started), at the cost that a question like "the program keeps looping and
    # won't stop" also hits. 「不斷循環」「反覆循環」「一直迴圈」 (loop endlessly /
    # loop repeatedly / keep on looping) have no such reason and are exactly how one
    # describes a stuck program (in Taiwan 「迴圈」 is the programming loop), so they
    # were removed 2026-09-22, by the same rule that leaves out 「進入循環」.
    "一直循環", "自走循環", "持續循環", "循環下去",
    "持續迴圈", "迴圈下去",
    # English deliberately leaves out phrasing that describes program behaviour
    # (removed 2026-09-22: `loop until` / `loop forever` / `keep looping` / a bare
    # `without asking`): "how do I loop until the list is empty", "why does it keep
    # looping" and "install without asking for confirmation" are all ordinary
    # programming questions, for the same reason the Chinese list leaves out
    # 「進入循環」.
    "don't ask me", "do not ask me", "without asking me",
    "keep going until", "until it is done", "until it's done", "until done",
    "do it autonomously", "work autonomously", "autonomously until",
    "loop mode", "autonomous mode",
)
# Compound words that contain a trigger word but mean something completely
# unrelated: masked out entirely before matching. 「自走砲」 (self-propelled gun) is
# a gaming and military term (exactly the kind of game the owner plays), 「自走式」
# (self-propelled) is an adjective for machinery, 「持續整合」 is CI, and
# 「持續時間」「持續性」 (duration / persistence) are ordinary nouns. Only the word
# itself is masked, so a separate 「自走模式」 in the same sentence still hits.
_DOROSSI_LOOP_INTENT_MASKS = (
    "自走砲", "自走炮", "自走式",
    "持續整合", "持續時間", "持續性",
)
# Phrases that span words, of the 「一直做到…完成」 (keep doing it … until done)
# kind: (head, tail, the gap must contain one of). Every entry goes through the
# same set of rules (`_dorossi_loop_pair_hit`), so a newly added pair gets them
# automatically -- do not write a one-off if:
#   1. Ordered -- the head comes before the tail;
#   2. The gap is at most `_DOROSSI_LOOP_PAIR_MAX_GAP` characters and does not
#      cross a sentence;
#   3. The gap contains none of `_DOROSSI_LOOP_PAIR_ENDPOINTS` -- 「先做到這裡為止就好」
#      (just do it up to here for now) and 「做到今天為止」 (do it until today) are
#      about where to stop, the exact opposite of 「做到完成為止」 (do it until
#      done); 「才」 is narrative (「一直做到半夜才完成」 "kept at it until midnight
#      before it was done", 「持續多久才完成」 "how long did it take to finish");
#   4. When the third field is non-empty, the gap must contain one of them --
#      「持續…完成」 needs 「到」 (直到完成 / 修到完成, "until done" / "fix until
#      done"), otherwise a question like 「持續整合的設定完成了嗎」 ("is the CI
#      setup done?") would count too.
# Before 2026-09-22 it counted whenever "the two strings appear anywhere, in any
# order"; measured, 「你目前為止做到哪裡了？」 ("how far have you got so far?") and
# 「先做到這裡為止就好」 both started an unattended loop; and across the whole test
# suite this branch had never once returned True (every test sentence with a pair
# was hit first by a single phrase), so nobody noticed.
_DOROSSI_LOOP_INTENT_PAIRS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("一直做到", "完成", ()),
    ("做到", "為止", ()),
    ("持續", "完成", ("到",)),
)
_DOROSSI_LOOP_PAIR_MAX_GAP = 24
_DOROSSI_LOOP_PAIR_ENDPOINTS = (
    "這裡", "這邊", "這兒", "這樣", "這步", "這一步", "這個階段", "此",
    "那裡", "那邊", "哪", "目前", "現在", "至今", "今天", "一半", "才",
)
_DOROSSI_LOOP_PAIR_SENTENCE_BREAKS = frozenset("。！？!?；;\n")

# When this mode's **name** appears in a question, it is usually asking about the
# feature, not giving an order (2026-09-22). The real case:
# 「現在是否已經支援 Discord 上的平行多個執行 /dorossi ask 或自走模式」 ("is running
# several /dorossi ask or self-loop mode in parallel on Discord supported now?")
# started a self-loop -- the name 「自走」 is a bare substring, and that sentence is
# a question. So for the name to count, the sentence must "not be a question", or
# be a question whose name is immediately preceded by an invoking verb and which
# is not asking how to do it (「可以進入自走模式幫我補完嗎」 "can you enter self-loop
# mode and finish it for me?" counts; 「要怎麼進入自走模式？」 "how do I enter
# self-loop mode?" does not). The ordinary order phrases (不要問我, 做到完成 …) are
# not affected by this.
_DOROSSI_LOOP_MODE_NAMES = (
    "自走", "自走循環", "循環模式", "迴圈模式", "loop mode", "autonomous mode",
)
_DOROSSI_QUESTION_MARKERS = (
    "？", "?", "嗎", "呢", "是否", "有沒有", "能不能", "可不可以", "會不會", "是不是",
    "支不支援",
)
_DOROSSI_HOW_MARKERS = (
    "怎麼", "如何", "為什麼", "為何", "什麼", "哪", "how ", "what ", "why ", "which ",
)
_DOROSSI_LOOP_INVOKE_VERBS = (
    "進入", "開啟", "啟動", "打開", "開", "用", "使用", "切到", "切換到", "改用", "改成", "以",
    "跑", "進", "enter ", "use ", "start ", "switch to ", "run in ", "turn on ", "go into ",
)


_DOROSSI_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\n])")


def _dorossi_loop_mode_name_counts(text: str, name: str) -> bool:
    """Whether the mode name `name` in `text` (already lower-cased) counts as an
    order. See the note above `_DOROSSI_LOOP_MODE_NAMES`.

    **Judged sentence by sentence**: a question mark and 「為什麼」 (why) govern only
    their own sentence -- in 「我不知道為什麼測試會紅？進入自走模式把它修好」 ("I don't
    know why the tests are red? Enter self-loop mode and fix it") the order is in the
    second sentence, and looking at the whole text at once would let the first
    sentence's question mark and 「為什麼」 swallow it."""
    for sentence in _DOROSSI_SENTENCE_SPLIT_RE.split(text):
        if name not in sentence:
            continue
        if not any(marker in sentence for marker in _DOROSSI_QUESTION_MARKERS):
            return True
        if any(marker in sentence for marker in _DOROSSI_HOW_MARKERS):
            continue
        start = sentence.find(name)
        while start != -1:
            before = sentence[max(0, start - 12):start]
            if any(before.endswith(verb) for verb in _DOROSSI_LOOP_INVOKE_VERBS):
                return True
            start = sentence.find(name, start + 1)
    return False


def _dorossi_loop_pair_hit(text: str, head: str, tail: str,
                           gap_needs: tuple[str, ...] = ()) -> bool:
    """Whether `text` holds a "`head` … (gap) … `tail`" stretch that satisfies the
    pair rules (the four listed above `_DOROSSI_LOOP_INTENT_PAIRS`). Every position
    where `head` occurs is tried, so an earlier stretch that does not count cannot
    block a real one later on."""
    start = text.find(head)
    while start != -1:
        gap_from = start + len(head)
        end = text.find(tail, gap_from)
        if end != -1:
            gap = text[gap_from:end]
            if (len(gap) <= _DOROSSI_LOOP_PAIR_MAX_GAP
                    and not any(ch in _DOROSSI_LOOP_PAIR_SENTENCE_BREAKS
                                for ch in gap)
                    and not any(w in gap for w in _DOROSSI_LOOP_PAIR_ENDPOINTS)
                    and (not gap_needs or any(w in gap for w in gap_needs))):
                return True
        start = text.find(head, start + 1)
    return False


def _dorossi_matches_loop_intent(prompt: str) -> bool:
    """Whether the question carries the self-loop intent of "finish it on your own,
    don't ask me". The matching is deliberately conservative: the cost of a false
    trigger is an unattended loop, while the cost of a miss is the owner having to
    rephrase more explicitly.

    ⚠️ Do not assume "a miss still gets a second chance from backend
    self-judgement": that path is controlled by `dorossi_self_judge_enabled`, and
    this machine's `bot_config.json` turns it off (found 2026-09-22), so on this
    host this function is the **only** way into the self-loop -- when tightening
    it, not a single real order phrasing may be dropped."""
    if not prompt:
        return False
    for mask in _DOROSSI_LOOP_INTENT_MASKS:
        prompt = prompt.replace(mask, "\x00")
    low = prompt.lower()
    for sub in _DOROSSI_LOOP_INTENT_SUBSTRINGS:
        if sub in prompt or sub in low:
            if (sub in _DOROSSI_LOOP_MODE_NAMES
                    and not _dorossi_loop_mode_name_counts(low, sub)):
                continue
            return True
    return any(_dorossi_loop_pair_hit(prompt, head, tail, needs)
               for head, tail, needs in _DOROSSI_LOOP_INTENT_PAIRS)


def _dorossi_remove_all_sentinels(text: str, markers: tuple[str, ...]) -> str:
    """Remove `markers` from `text` until **not one is left**, rather than in a
    single pass.

    `str.replace` stops after one pass, but the removal itself joins the left and
    right sides together, so a sandwich (`<<<DOROSSI-LOOP-` + one complete sentinel
    + `DONE>>>`) **reassembles** a complete sentinel once the inner one is removed,
    and a single replace can therefore still leave a sentinel behind. The entire
    reason these strippers exist is "not one may be left before sending", so this
    scans until nothing changes.

    The two sentinels share the long prefix `<<<DOROSSI-LOOP-`, and removing one
    can also assemble the other, so it is "scan every marker once per pass, and
    stop only after a whole pass with no change", not each marker converging on its
    own. Every effective replacement makes the string shorter, so it always stops.
    """
    while True:
        before = text
        for marker in markers:
            text = text.replace(marker, "")
        if text == before:
            return text


def _dorossi_strip_loop_sentinel(answer: str) -> tuple[str, bool]:
    """Return (cleaned, done). done is True when this round's output contains the
    completion sentinel; cleaned has every occurrence of the sentinel removed and
    surrounding whitespace trimmed. Used on "the final per-round answer" to decide
    whether to end the loop, and to make sure the sentinel never appears in the
    outward reply."""
    if not answer or DOROSSI_LOOP_SENTINEL not in answer:
        return answer, False
    return _dorossi_remove_all_sentinels(
        answer, (DOROSSI_LOOP_SENTINEL,)).strip(), True


def _dorossi_strip_open_sentinel(answer: str) -> tuple[str, bool]:
    """Return (cleaned, opened). opened is True when the turn-1 output contains the
    "open sentinel" (the backend judged this to be a larger task needing several
    autonomous rounds); cleaned has every occurrence of the sentinel removed and
    surrounding whitespace trimmed. Used on the turn-1 answer of the backend
    self-judgement path, to make sure the open sentinel never appears in the
    outward reply."""
    if not answer or DOROSSI_LOOP_OPEN_SENTINEL not in answer:
        return answer, False
    return _dorossi_remove_all_sentinels(
        answer, (DOROSSI_LOOP_OPEN_SENTINEL,)).strip(), True


# Every internal sentinel the streaming preview must strip (completion + open).
# Both are internal loop / self-judgement signals and must never flash by in the
# live preview.
_DOROSSI_STREAM_SENTINELS = (DOROSSI_LOOP_SENTINEL, DOROSSI_LOOP_OPEN_SENTINEL)


def _dorossi_redact_sentinel_stream(text: str) -> str:
    """For the streaming preview: remove sentinels that have fully appeared (both
    completion and open), and also hide "a half sentinel (prefix) at the end", so a
    half-streamed sentinel never flashes by in the live preview. Only the trailing
    prefix is touched; the body is unaffected. The two sentinels share a long
    prefix, but the end can only be a prefix of one of them; each is checked in
    turn and the first hit returns, so they cannot interfere with each other."""
    if not text:
        return text
    text = _dorossi_remove_all_sentinels(text, _DOROSSI_STREAM_SENTINELS)
    for sentinel in _DOROSSI_STREAM_SENTINELS:
        for i in range(len(sentinel) - 1, 0, -1):
            if text.endswith(sentinel[:i]):
                return text[:-i]
    return text


_dorossi_client = None  # lazily-constructed AsyncAnthropic singleton

# The api backend's per-request timeout and the SDK's own retry count. **Spelling
# them out is deliberate, not a copy of the defaults.**
#
# This path has no streaming, so neither layer of the claude_code two-layer
# watchdog (the idle layer + the hard wall clock) applies here:
# `messages.create()` is a single await, and its only bound is the SDK's timeout.
# And `anthropic` is not pinned in `requirements.txt` (a fresh clone getting the
# latest is deliberate), so "the bound" equals "this SDK version's default" --
# not a decision this project made, and one that changes silently on upgrade. An
# unattended self-loop should least of all have a time limit that drifts on its
# own.
#
# These two values are **exactly the defaults of anthropic 1.3.0 / 1.4.0** (read
# timeout 600s, 2 retries), so writing them out does not change behaviour; it only
# turns them from "inherited" into "chosen".
#
# **But "worst case 600 × (1 + 2) = 30 minutes" does not hold on these two values
# alone (2026-09-19).** **Between** two retries there is also the SDK's own sleep,
# and anthropic 1.6.0 changed it: `_calculate_retry_timeout` used to follow the
# server's `Retry-After` only when `0 < retry_after <= 60`, otherwise falling back
# to its own exponential backoff (at most 8 seconds); from 1.6.0 on it is
# `min(retry_after, 4_294_967.0)`, i.e. **it sleeps however long the server
# says**. Measured against a local fake server with 429 + `retry-after: 3600`:
# 1.4.0 retries once each after 0.4 / 0.8 seconds and raises `RateLimitError` at
# 1.3 seconds; 1.7.0 sleeps 3600 seconds before each retry, stalling a round for
# about 2 hours. 1.7.0 is what a fresh clone gets today. The consequences come in
# two layers: this round holds the session lock stuck for two hours; and the bot's
# own usage-limit handling (`reset_at = now + retry_after`) only gets the exception
# once the SDK has finished sleeping, so the reset time it computes is also **late
# by the stretch already slept away**.
#
# So there are now two layers, both this project's own, which do not drift with
# the SDK version:
#   1. `_dorossi_api_clamp_retry_after` (the http client's response hook): when the
#      wait the server asks for exceeds `_DOROSSI_API_SDK_SLEEP_CAP_SEC` (60
#      seconds, exactly the SDK's own cap before 1.6.0), it adds
#      `x-should-retry: false` to the response, so the SDK does not retry on its
#      own and raises at once, with the `retry-after` header left intact on the
#      exception for `_dorossi_api_retry_after_sec` to read. Short waits, and
#      5xx / 529 with no wait, are still retried by the SDK as before.
#   2. `_dorossi_api_call_ceiling_sec()`: `_dorossi_via_api` wraps the whole
#      `messages.create()` in `asyncio.timeout`. The 30-minute figure above was
#      never precise anyway -- 600 is the read timeout (the gap between bytes), not
#      the total length of one request -- so "how long at worst" must be enforced
#      by this project itself, not inferred from the SDK's parameters.
# Whether to tighten these numbers is the owner's tuning decision (twice as wide
# as the claude_code chat-only hard limit `dorossi_cc_hard_limit_off_sec` = 900s);
# it is written here to make that decision visible.
DOROSSI_API_TIMEOUT_SEC = 600.0
DOROSSI_API_MAX_RETRIES = 2
# How many seconds the SDK's built-in retry may sleep **each time** at most (see
# layer 1 above). 60 is not a newly picked number: it is the cap the SDK
# hard-coded before anthropic 1.6.0, so on 1.4.0 the only effect of this layer is
# "no retry beyond 60 seconds", and no short wait that would have happened
# disappears.
_DOROSSI_API_SDK_SLEEP_CAP_SEC = 60.0
# Extra slack for the outer frame: connection setup, scheduling jitter.
# Deliberately small -- the outer frame is there to "bound the worst case", not to
# "just fit the slowest normal round".
_DOROSSI_API_CEILING_SLACK_SEC = 30.0


def _dorossi_api_call_ceiling_sec() -> float:
    """How many seconds one `messages.create()` (including the SDK's built-in
    retries) may run at most.

    = per-request timeout × number of requests + at most
    `_DOROSSI_API_SDK_SLEEP_CAP_SEC` of sleep before each retry + slack. By default
    600 × 3 + 60 × 2 + 30 = 1950 seconds. **The module constants are read at call
    time**, not written as default arguments (a default argument is bound when the
    `def` runs, so changing the constant later would have no effect)."""
    retries = max(0, int(DOROSSI_API_MAX_RETRIES))
    return (float(DOROSSI_API_TIMEOUT_SEC) * (1 + retries)
            + retries * _DOROSSI_API_SDK_SLEEP_CAP_SEC
            + _DOROSSI_API_CEILING_SLACK_SEC)


def _dorossi_sdk_retry_wait_sec(headers, *, now: float | None = None) -> float | None:
    """How many seconds the SDK's built-in retry **would** plan to sleep given this
    set of response headers. None = the headers give no wait.

    **Copies the SDK's own `_parse_retry_after_header` step by step** (identical
    word for word in 1.4.0 and 1.7.0), including its precedence and quirks, because
    the question this answers is "what will the SDK do", not "what does the server
    mean":
      1. if `retry-after-ms` (non-standard, milliseconds) converts to float, use it
         -- **even if it is nan or negative**, the SDK looks no further;
      2. otherwise, if `retry-after` converts to float, take it as seconds (the SDK
         allows fractions);
      3. otherwise treat `retry-after` as an HTTP date (`email.utils.parsedate_tz` +
         `mktime_tz`, read as local time when there is no time zone -- that is what
         the SDK does), returning "that moment minus now", which may be negative.
    The return value may be nan / inf / negative, and **the caller judges it**
    (`_dorossi_api_clamp_retry_after` only asks "is it > 60", which nan naturally
    fails and inf naturally passes, matching the SDK's `retry_after > 0`).

    Deliberately **not** used to replace `_dorossi_api_retry_after_sec`: that one
    answers "is the value worth scheduling a wait on", accepts only finite positive
    seconds and never an HTTP date (pinned by a test). The two ask two different
    questions. If the SDK's private implementation ever changes, the comparison test
    in `test_dorossi_api_retry` asks both sides with the same corpus. Never raises
    -- this runs inside the http client's hook, and blowing up there would turn a
    normal response into an exception.
    """
    try:
        if headers is None:
            return None
        try:
            return float(headers.get("retry-after-ms", None)) / 1000
        except (TypeError, ValueError):
            pass
        raw = headers.get("retry-after")
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
        parsed = email.utils.parsedate_tz(raw)
        if parsed is None:
            return None
        when = email.utils.mktime_tz(parsed)
        return float(when - (time.time() if now is None else now))
    except Exception:  # pylint: disable=broad-except
        return None


async def _dorossi_api_clamp_retry_after(response) -> None:
    """The http client's response event hook: when the wait the server asks for
    exceeds `_DOROSSI_API_SDK_SLEEP_CAP_SEC`, tell the SDK not to retry on its own.

    It does so by adding `x-should-retry: false` to the response -- the first thing
    the SDK's `_should_retry` looks at is this header (in both 1.4.0 and 1.7.0), and
    on `"false"` it does not retry and raises the error straight up. `retry-after`
    itself is **left untouched**, so `_dorossi_via_api` can still read it from the
    exception, and `_DorossiUsageLimitError.reset_at` is simply "now + the seconds
    the server asked for", no longer late by the stretch the SDK has already slept.

    **Not limited to 429**: from 1.6.0 on, every status code that gets retried
    (408 / 409 / 429 / 5xx) sleeps the full `Retry-After`, so the criterion is
    "would the SDK sleep more than 60 seconds", not the status code. 5xx / 529 with
    no wait (or a wait ≤ 60 seconds) is still retried by the SDK with its own
    backoff. 2xx is left alone.

    Must be async (`AsyncClient` awaits every hook). **Never raises**: an exception
    thrown by a hook would turn a response that could have been handled normally
    into a new, baffling exception. On trouble only the type name goes to stderr --
    that line lands in `discord_bot.log`, and `/log tail` is that file's outward
    exit."""
    try:
        status = getattr(response, "status_code", None)
        if not isinstance(status, int) or status < 400:
            return
        wait = _dorossi_sdk_retry_wait_sec(response.headers)
        if wait is None or not wait > _DOROSSI_API_SDK_SLEEP_CAP_SEC:
            return
        response.headers["x-should-retry"] = "false"
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] retry-after clamp skipped ({type(exc).__name__})",
              file=sys.stderr)


def _dorossi_api_http_client():
    """An http client carrying `_dorossi_api_clamp_retry_after`; None when the SDK
    offers no factory.

    It uses the SDK's own `DefaultAsyncHttpxClient` (public API), so the connection
    limits, default timeout and redirect following are exactly the same as without
    `http_client` (measured 2026-09-19 on 1.4.0 / 1.7.0: connection limit 1000 and
    timeout 600 on both) -- the only difference is that hook."""
    factory = getattr(anthropic, "DefaultAsyncHttpxClient", None) if anthropic else None
    if factory is None:
        return None
    return factory(event_hooks={"response": [_dorossi_api_clamp_retry_after]})


def _get_dorossi_client():
    """Lazily build + cache the Anthropic async client. Returns None if the SDK
    is missing or no credentials can be resolved (construction is offline, so a
    failure here means an auth/config problem, not a network one).

    The timeout and retry count are always spelled out (see
    `DOROSSI_API_TIMEOUT_SEC`): this path has no streaming, the SDK's timeout is
    the only time bound, and it must not drift with the dependency's defaults.
    The http client carries the retry-after clamp (layer 1 of the same note); when
    the clamp cannot be installed the client is still built and one line goes to
    stderr -- the outer frame (layer 2) still guards the worst-case time, and an
    accuracy improvement is no reason to shut the whole backend down."""
    global _dorossi_client
    if _dorossi_client is not None:
        return _dorossi_client
    if AsyncAnthropic is None:
        return None
    kwargs = {"timeout": DOROSSI_API_TIMEOUT_SEC,
              "max_retries": DOROSSI_API_MAX_RETRIES}
    try:
        http_client = _dorossi_api_http_client()
    except Exception as exc:  # pylint: disable=broad-except
        http_client = None
        print(f"[dorossi] retry-after clamp could not be installed "
              f"({type(exc).__name__}); long server-requested waits are still "
              f"bounded by the {_dorossi_api_call_ceiling_sec():.0f}s call ceiling",
              file=sys.stderr)
    if http_client is not None:
        kwargs["http_client"] = http_client
    try:
        _dorossi_client = AsyncAnthropic(**kwargs)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] client init failed: {exc!r}", file=sys.stderr)
        return None
    return _dorossi_client


class _DorossiResumeError(RuntimeError):
    """Raised when `claude -p --resume <id>` fails because the stored session is
    gone (expired / cleaned). The caller retries once with a fresh session."""


class _DorossiTransientError(RuntimeError):
    """The backend returned a **server-side error that usually clears by itself**
    (529 Overloaded, 502/503/504 and the like) -- not a stale session, and not the
    plan's usage limit.

    Added 2026-09-03. Before this, such an error travelled all the way into a
    `RuntimeError` that fell into the self-loop's generic `except`, so **the whole
    unattended loop stopped on the spot** -- while the error message itself said
    "usually temporary — try again in a moment". Measured: two 529s in a row at
    21:31 and 21:36 (the "retry" in between threw the session away and started a new
    one, which does nothing at all for a server overload), and the second one ended
    the run.

    `status` is for the code to judge (how long to wait before retrying), `reason`
    is a short string for the log -- **do not** send it straight to the chat
    platform; it is uncontrolled external text (Layer 1).
    """

    def __init__(self, reason: str, *, status: int | None = None,
                 session_id: str | None = None) -> None:
        super().__init__(reason)
        self.status = status
        self.session_id = session_id


# HTTP statuses that clear by themselves. 429 is **not** here: that is a usage /
# rate limit, which `_dorossi_cc_usage_limit` routes down its own waiting path
# (there is a reset time to wait for).
DOROSSI_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504, 529})

# Text markers. The status code is sometimes not carried into the result event,
# leaving only the English message.
_TRANSIENT_TEXT_MARKERS = (
    "overloaded",            # 529 Overloaded / overloaded_error
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "temporarily unavailable",
)


class _DorossiOfflineError(RuntimeError):
    """The backend **cannot reach its server**: DNS resolution failed, the
    connection was refused / reset, the network is unreachable.

    Added 2026-09-22. That day, 17:32–18:37, the local DNS was down the whole time,
    and the backend CLI returned
    `API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)`,
    a class that had no classification before: it fell all the way to the last
    branch of the verdict and was treated as "stale session" -- **the context was
    thrown away and a new session retried** (which of course could not connect
    either), then the self-loop's generic retry gave up after three tries, and all
    six running tasks stopped where they stood.

    It is handled on the same principle as the usage limit and transient failures:
    **wait, and rerun with the same session**. The difference is "wait until when":
    here it waits for the network to come back (the caller polls connectivity), not
    a guessed number of seconds, and **with no attempt limit** -- the owner ruled
    out any round / spending limits, so waiting for the network can only be ended
    by abort.

    `backend` lets the caller decide which host to probe; `reason` is a short
    string for the log -- **do not** send it to the chat platform (Layer 1).
    `session_id` is the one **passed in** to this invocation (for resuming).
    """

    def __init__(self, reason: str, *, backend: str | None = None,
                 session_id: str | None = None) -> None:
        super().__init__(reason)
        self.backend = backend
        self.session_id = session_id


# Text markers for "cannot reach the server" (matched lower-case). They come from
# two sources: the CLI's result text (measured 2026-09-22: `API Error: Can't reach
# the API server — check your internet or DNS (ENOTFOUND)`), and the low-level
# error codes the CLI / SDK writes to stderr or to error events.
_OFFLINE_TEXT_MARKERS = (
    "can't reach the api server",
    "cannot reach the api server",
    "unable to connect to api",
    "enotfound",
    "eai_again",
    "econnreset",
    "econnrefused",
    "etimedout",
    "enetunreach",
    "ehostunreach",
    "getaddrinfo",
    "network is unreachable",
    "failed to lookup address",
    "dns error",
    "error sending request for url",
)
# The length cap for the result-text branch: the CLI's notice is a one-line
# template, while an answer that talks about "connection errors" is prose. Same
# reason as the usage-limit and not-logged-in branches (a long answer that happens
# to discuss ECONNRESET must not be judged offline).
_DOROSSI_OFFLINE_NOTICE_MAX_CHARS = 300


def _dorossi_offline_marker_in(text) -> bool:
    """Whether `text` contains "cannot reach the server" wording. Never raises."""
    try:
        low = str(text or "").lower()
    except Exception:  # pylint: disable=broad-except
        return False
    return any(marker in low for marker in _OFFLINE_TEXT_MARKERS)


def _dorossi_cc_offline(result_ev, err: str = "", session_id: str | None = None
                        ) -> "_DorossiOfflineError | None":
    """Was this rc != 0 round a "cannot reach the server"? If so return a filled-in
    exception, otherwise None.

    Evidence comes from two places: the `result` event (`is_error` true, text short
    enough to be a notice) and stderr. A successfully completed result never is
    (`_claude_result_succeeded`). Pure function, never raises."""
    try:
        if _claude_result_succeeded(result_ev):
            return None
        ev = result_ev if isinstance(result_ev, dict) else {}
        text = ev.get("result")
        hit = (ev.get("is_error") and isinstance(text, str)
               and len(text.strip()) <= _DOROSSI_OFFLINE_NOTICE_MAX_CHARS
               and _dorossi_offline_marker_in(text))
        if not hit and _dorossi_offline_marker_in(err):
            hit = True
        if not hit:
            return None
        return _DorossiOfflineError(
            (str(text or "") or str(err or ""))[:400] or "offline",
            backend="claude_code", session_id=session_id)
    except Exception:  # pylint: disable=broad-except
        return None


def _dorossi_codex_offline(text, session_id: str | None = None
                           ) -> "_DorossiOfflineError | None":
    """Whether codex's stderr / failure event looks like "cannot reach the server".
    Called only from the rc != 0 path, and the text fed in contains no answer (see
    the note on `_dorossi_codex_usage_limit`)."""
    if not _dorossi_offline_marker_in(text):
        return None
    return _DorossiOfflineError(str(text)[:400], backend="codex",
                                session_id=session_id or None)


def _dorossi_api_is_offline(exc) -> bool:
    """Whether an SDK exception is a connection-layer failure
    (`APIConnectionError`). Never raises.

    **A timeout (`APITimeoutError`, a subclass of `APIConnectionError`) does not
    count**: that means the connection was made and the other side never answered,
    not that the network is down -- waiting for the network will not make it
    better, so it keeps taking the existing exception path (`test_dorossi_api_retry`
    pins it being re-raised as is). Anything with a status code (the
    `APIStatusError` family) never is either -- the server answered, so it goes
    through the existing usage-limit / transient-failure / other classifications."""
    try:
        timeout_cls = getattr(anthropic, "APITimeoutError", None) if anthropic else None
        if timeout_cls is not None and isinstance(exc, timeout_cls):
            return False
        conn = getattr(anthropic, "APIConnectionError", None) if anthropic else None
        if conn is not None and isinstance(exc, conn):
            return True
        return _dorossi_offline_marker_in(f"{type(exc).__name__}: {exc}") and \
            getattr(exc, "status_code", None) in (None, "")
    except Exception:  # pylint: disable=broad-except
        return False


def _dorossi_cc_transient_error(result_ev: dict, answer: str = "",
                                session_id: str | None = None
                                ) -> "_DorossiTransientError | None":
    """Whether the `result` event looks like "a transient server failure"; if so
    return a filled-in exception, otherwise None.

    Same shape as `_dorossi_cc_usage_limit`, **and it must be called after it**: a
    usage limit can also carry a status code other than 5xx, but it has its own
    waiting strategy (wait until the quota resets), far more precise than the
    exponential backoff here.
    """
    if not isinstance(result_ev, dict):
        result_ev = {}
    status = result_ev.get("api_error_status")
    try:
        status_int = int(status) if status not in (None, "") else None
    except (TypeError, ValueError):
        status_int = None
    haystack = " ".join(
        str(result_ev.get(k) or "")
        for k in ("subtype", "result", "terminal_reason")
    )
    if answer:
        haystack += " " + str(answer)
    low = haystack.lower()
    hit = (status_int in DOROSSI_TRANSIENT_STATUSES
           or any(marker in low for marker in _TRANSIENT_TEXT_MARKERS))
    if not hit:
        return None
    return _DorossiTransientError(
        (haystack.strip() or f"api_error_status={status_int}")[:400],
        status=status_int, session_id=session_id)


def _dorossi_transient_wait_seconds(attempt: int, *, base: float = 30.0,
                                    cap: float = 900.0) -> float:
    """How many seconds to wait after the `attempt`-th (counting from 1)
    consecutive transient failure: exponential backoff, capped at `cap`.

    It starts at 30 seconds rather than a few: hitting an overloaded server again
    right away only makes it worse, and an unattended task is in no hurry to
    recover within ten seconds. The cap is 15 minutes, so a long service outage
    does not turn into idling beyond every 15 minutes, nor wait so long that it
    misses the recovery.
    """
    try:
        n = max(1, int(attempt))
    except (TypeError, ValueError):
        n = 1
    return float(min(cap, base * (2 ** (n - 1))))


class _DorossiLoopSilence(RuntimeError):
    """Raised when an autonomous-loop round is cut because there was no new
    output for the configured silence window. This is the loop's per-round
    backstop and fires even while a tool is still 'executing' (unlike the normal
    idle tier, which waits for pending tools) — so a hung tool in an unattended
    loop can't suppress the watchdog forever.

    Since 2026-09-05 the loop **respawns a few times before giving up**
    (`dorossi_silence_retry_max`, default 2, backoff 20s→40s): what hangs is
    usually that backend process, and a fresh one often gets through, whereas
    before that a single stall meant the whole unattended task stood still waiting
    for someone to pick it up. It stops only after hanging more times in a row than
    the limit -- by then it no longer looks sporadic. Setting it to 0 restores the
    old "stop on the first silence" behaviour."""


class _DorossiUsageLimitError(RuntimeError):
    """Raised when a Dorossi turn fails because the underlying plan / quota usage
    limit was hit (not a transient or stale-session failure, so the fresh-session
    retry must be skipped).

    The three fields are deliberately split into "for people" and "for code"; do
    not merge them:

    * `reset_hint` -- the reset-time string **for people**, already narrowed by
      `_dorossi_sanitize_reset_hint` to a known-safe shape (it is the only raw
      backend text this module lets into a Discord reply). Untrusted and not to be
      computed with; `None` means there is no hint to show.
    * `reset_at` -- epoch seconds **for code**, set only when the backend gives an
      explicit timestamp (the `…|<epoch>` variant, or the API backend's
      `retry-after` header). The self-loop uses it to decide how long to sleep;
      without one it is `None` and the caller probes with backoff. **A
      human-readable "resets 3:45pm" is not converted into `reset_at`** -- that
      form has no time zone, and guessing 5 hours wrong costs far more than one
      extra probe; the reasoning is in `_dorossi_extract_reset_epoch`.
    * `session_id` -- the session id the backend had advanced to at the moment it
      hit the limit (may be `None`). **This is the key to "waiting and then carrying
      on loses no work"**: a usage limit is usually hit "halfway through", and if
      this id is not saved back to the slot, the resume after the quota returns
      uses the previous round's old id, and everything this round already did is
      wasted.
    """

    def __init__(self, message: str, reset_hint: str | None = None, *,
                 reset_at: float | None = None,
                 session_id: str | None = None) -> None:
        super().__init__(message)
        self.reset_hint = reset_hint
        self.reset_at = reset_at
        self.session_id = session_id


class _DorossiWorkdirError(NotADirectoryError):
    """Before spawning, the working directory turned out to be no longer a usable
    directory (raised by `_dorossi_require_workdir`).

    **It deliberately inherits `NotADirectoryError`, unlike its siblings that
    inherit `RuntimeError`.** Before the fix this situation was exactly the
    `NotADirectoryError` the child process raised, and `dorossi_error_is_fatal`
    already judges that type fatal (the self-loop stops at once instead of
    retrying three rounds for nothing); any `except OSError` still catches it too.
    This merely raises the same thing earlier and by name, so `_dorossi_error_hint`
    can tell it apart and give the right reason. As a `RuntimeError`, a permanent
    condition would be retried three rounds by the loop before stopping.

    The message is a fixed short English sentence with no path; it is never sent to
    the chat platform (the bot assembles outward strings).
    """


class _DorossiCliOptionError(RuntimeError):
    """The backend CLI rejected one of the options we passed it **before starting
    the round** (`unknown option`).

    This almost certainly means "the installed CLI is older than this code": the
    project adds flags as the CLI gains features (for example `--tools ""` added for
    chat-only on 2026-09-19), and an older command-line parser exits with rc=1 on
    an option it does not recognise, with not a single character on stdout and
    `error: unknown option '<flag>'` on stderr (measured the same day by feeding
    2.1.276 a nonexistent option).

    There are two reasons for a separate type: (1) do not fall into the resume
    retry -- a fresh session fails in exactly the same way, only spawning one more
    process and printing one more baffling log line; (2) a retry has no chance of
    succeeding, so it is listed in `_FATAL_ERROR_TYPES` and the unattended loop
    need not try three rounds for nothing.
    `option` is the rejected flag name (already narrowed to a flag's shape by
    `_CLI_UNKNOWN_OPTION_RE`). The message is fixed English plus the flag name, with
    no path; the bot assembles outward strings (always generic).
    """

    def __init__(self, option: str) -> None:
        super().__init__(f"claude -p does not accept the option {option!r}")
        self.option = option


class _DorossiAuthError(RuntimeError):
    """The backend CLI **has no usable sign-in**: not logged in, credentials
    expired, or forced into bare mode where it cannot read the sign-in.

    Added 2026-09-19. Measured (CLI 2.1.276): both shapes are rc=1 + one result
    with `is_error` true, and before this they fell into the last two branches:
    with a session -> `_DorossiResumeError` -> the caller **throws the
    conversation away** and opens a new one (which fails in exactly the same way)
    -> `RuntimeError`, and that text ("Not logged in · Please run /login", "Failed
    to authenticate. API Error: 401 …") hits none of `_FATAL_ERROR_MARKERS`, so the
    self-loop retried three more rounds (20 / 40 / 80 second backoff). The 401 kind
    first lets the CLI retry ten times on its own on every call (measured 190
    seconds), and each round is two calls, resume + fresh. None of them has any
    chance of succeeding: the sign-in does not come back by itself between retries.

    Listed in `_FATAL_ERROR_TYPES` (retrying is useless), and the verdict comes
    **before** the resume retry (throwing the conversation away is useless too).
    `evidence` is a label for what the verdict rested on (`api_error_status=401`,
    `api_retry=authentication_failed`, `result text`), composed by the verdict from
    a fixed vocabulary and containing none of the CLI's raw text; `bare_suspect` =
    the init event has no `memory_paths` (looks like bare mode).

    The message deliberately contains **none** of the `_FATAL_ERROR_MARKERS`
    wording: the fatal verdict must hold by type, not because the message happens
    to contain some word -- otherwise removing it from `_FATAL_ERROR_TYPES` would
    turn no test red. The bot assembles outward strings (always generic).
    """

    def __init__(self, evidence: str, *, bare_suspect: bool = False) -> None:
        super().__init__(f"claude -p has no usable sign-in ({evidence})")
        self.evidence = evidence
        self.bare_suspect = bare_suspect


# --- Multi-session store --------------------------------------------------
# The on-disk store is `{uid: <user-record>}` where each user record is:
#   {"active": "<sid>" | None,    # currently-selected session id
#    "next_seq": <int>,           # next per-user counter → id "s<next_seq>"
#    "sessions": {"<sid>": {cc_session_id?, cc_cwd?, cc_extra_dir?,
#                           api_history?, label?, created_at?, last_used?}}}
# Session ids are "s1", "s2", … (per-user incrementing; a deleted number is NOT
# reused within the user's lifetime). The legacy flat record (a bare session
# dict with no "sessions" key) is auto-migrated to this shape on load, wrapped
# as session "s1" so the user's existing conversation survives unchanged.
_DOROSSI_SESSION_ID_RE = re.compile(r"^s(\d+)$")
# A session label is user-supplied free text shown back in the list. Sanitize
# (strip newlines/backticks, cap length) so it can't break the rendered list.
DOROSSI_SESSION_LABEL_MAX = 50


def _dorossi_is_session_id(token: str) -> bool:
    """True iff `token` is a session id (`s` followed by digits). Reserved word
    `new` never matches, so it can't collide with an id."""
    return bool(token) and bool(_DOROSSI_SESSION_ID_RE.match(token))


def _dorossi_session_sort_key(sid: str):
    """Sort ids by their numeric part (s2 < s10); non-ids sort last."""
    m = _DOROSSI_SESSION_ID_RE.match(sid)
    return (0, int(m.group(1))) if m else (1, sid)


def _dorossi_clean_label(raw: str | None) -> str | None:
    """Normalize a user-provided label for safe display, or None if blank."""
    if not raw:
        return None
    cleaned = raw.replace("`", "'").replace("\n", " ").replace("\r", " ").strip()
    if not cleaned:
        return None
    return cleaned[:DOROSSI_SESSION_LABEL_MAX]


def _dorossi_parse_session_new(tail: str | None) -> tuple[str | None, str | None]:
    """Split the arguments of `/dorossi session new` into `(label, cwd)` (pure
    function, never raises).

    Syntax: `new [label] [cwd=<directory>]`
      * The label is free text and may contain spaces, `,`, `(`, `:` -- so the
        working directory is recognised only by the `cwd=` key (case-insensitive),
        not by position.
      * **Everything after** `cwd=` is the path: the path itself may contain
        spaces, `:`, `\\`, is always kept verbatim and never lower()-ed. So the
        label must come before `cwd=`.
      * The path is **not validated** here; the caller applies it only after
        `_dorossi_validate_dir` confirms "an existing directory", and a rejected raw
        string is written only to stderr (the path is never echoed outward).

    Only `cwd=` is recognised, deliberately: `dir=` (the extra accessible scope of
    `--add-dir`) has a different meaning in `_dorossi_parse_reset` and is not folded
    into this grammar -- for an extra scope use `/dorossi allowdir add`.
    """
    text = (tail or "").strip()
    if not text:
        return None, None
    idx = text.lower().find("cwd=")
    if idx == -1:
        return (text or None), None
    label = text[:idx].strip()
    cwd = text[idx + len("cwd="):].strip()
    return (label or None), (cwd or None)


def _dorossi_empty_user() -> dict:
    """A fresh user record with no sessions yet."""
    return {"active": None, "next_seq": 1, "sessions": {}}


def _dorossi_int_or_none(value):
    """Return `value` if it is a real int, otherwise None.

    **`bool` must be excluded separately**: it is a subclass of `int`, `True` would
    become 1 all the way through, and `f"s{seq}"` would then produce `"sTrue"` -- an
    id `_DOROSSI_SESSION_ID_RE` does not recognise. `CLAUDE.md` states the same rule
    for the two config loaders; this is the third place.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _dorossi_derive_next_seq(sessions: dict) -> int:
    """Next counter value that is strictly greater than every existing id, so a
    rebuilt counter never re-issues a live id."""
    mx = 0
    for sid in sessions or {}:
        m = _DOROSSI_SESSION_ID_RE.match(str(sid))
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1


def _dorossi_migrate_user(rec) -> dict:
    """Normalize one user record to the multi-session shape. A legacy flat dict
    (no "sessions" key) is wrapped as session "s1" so its conversation survives;
    garbage becomes an empty user. Never raises (caller guards too).

    Normalisation also drops **malformed slots** in `sessions` -- entries whose
    value is not a dict (or whose key is not a string). `dorossi_session.json` is a
    local file the owner can edit by hand, and every reader (the session list, the
    snapshot each Q&A round takes, the export) calls `.get(...)` directly on the
    slot, so one hand-made malformed slot would make all of those commands blow up
    with AttributeError -- exactly when the owner needs them most.

    It is caught here rather than guarded once per reader because this is the
    store's **single normalisation entry point**: `_dorossi_load_state` always goes
    through `_dorossi_migrate_state`, which calls this function for every uid.
    Catch it once and every reader benefits; scattered guards would miss the next
    reader added, and the missed one would show no symptom until someone actually
    hand-edits that file.

    The drop happens **before** `active` / `next_seq` are repaired, so `active`
    cannot point at an id that was just dropped (the existing
    `if active not in sessions` is naturally right once the order is right). Output
    for normal data is completely unchanged: with no malformed slot even the
    `sessions` dict object itself is reused as is, not rebuilt.
    """
    if not isinstance(rec, dict):
        return _dorossi_empty_user()
    sessions = rec.get("sessions")
    if isinstance(sessions, dict):
        bad = [sid for sid, sess in sessions.items()
               if not isinstance(sid, str) or not isinstance(sess, dict)]
        if bad:
            sessions = {sid: sess for sid, sess in sessions.items()
                        if isinstance(sid, str) and isinstance(sess, dict)}
            # Print only keys shaped like a session id. Other keys come from a
            # hand-edited file and may hold anything (host paths, prompt
            # fragments), and this stderr line lands in discord_bot.log, from where
            # the log query command sends it into the chat platform -- with only a
            # pattern-matching scrubber in between, which cannot recognise shapes
            # it has never seen. Slot contents are never printed.
            shown = sorted(sid for sid in bad
                           if isinstance(sid, str)
                           and _DOROSSI_SESSION_ID_RE.match(sid))
            note = ",".join(shown) if shown else "(none)"
            if len(shown) != len(bad):
                note += f" +{len(bad) - len(shown)} withheld"
            print(f"[dorossi] dropped {len(bad)} malformed session slot(s); "
                  f"ids={note}", file=sys.stderr)
        # Already new shape — repair active/next_seq defensively.
        next_seq = _dorossi_int_or_none(rec.get("next_seq"))
        if next_seq is None or next_seq <= _dorossi_derive_next_seq(
                sessions) - 1:
            next_seq = max(_dorossi_derive_next_seq(sessions),
                           next_seq if next_seq is not None else 1)
        active = rec.get("active")
        if active not in sessions:
            active = None
        return {"active": active, "next_seq": next_seq, "sessions": sessions}
    # Legacy flat record → wrap the whole dict as session "s1" (preserve it).
    return {"active": "s1", "next_seq": 2, "sessions": {"s1": dict(rec)}}


def _dorossi_migrate_state(state) -> dict:
    """Normalize the whole store to the multi-session shape in place. Robust to a
    missing/corrupt file or any garbage entry (never raises)."""
    if not isinstance(state, dict):
        return {}
    for uid in list(state.keys()):
        try:
            state[uid] = _dorossi_migrate_user(state.get(uid))
        except Exception:  # pylint: disable=broad-except
            state[uid] = _dorossi_empty_user()
    return state


def _dorossi_user_record(state: dict, uid: str) -> dict:
    """Return the uid's user record (multi-session shape), creating an empty one
    if absent/malformed. The returned dict is the live slot inside `state`."""
    rec = state.get(uid)
    if not isinstance(rec, dict) or not isinstance(rec.get("sessions"), dict):
        rec = _dorossi_empty_user()
        state[uid] = rec
    return rec


def _dorossi_new_session(rec: dict, label: str | None = None) -> str:
    """Allocate a new session under `rec`, mark it active, return its id. Uses
    the per-user counter (deleted numbers are NOT reused); defensively skips any
    id that somehow already exists."""
    seq = _dorossi_int_or_none(rec.get("next_seq"))
    if seq is None or seq < 1:
        seq = _dorossi_derive_next_seq(rec.get("sessions") or {})
    sessions = rec.setdefault("sessions", {})
    sid = f"s{seq}"
    while sid in sessions:
        seq += 1
        sid = f"s{seq}"
    now = time.time()
    sess: dict = {"created_at": now, "last_used": now}
    if label:
        sess["label"] = label
    sessions[sid] = sess
    rec["next_seq"] = seq + 1
    rec["active"] = sid
    return sid


def _dorossi_active_session(state: dict, uid: str) -> tuple[str, dict]:
    """Return (session_id, session_dict) for the uid's active session, creating
    a fresh active session (and the user record) if there is none. The returned
    dict is the live slot inside `state` — mutate it then `_dorossi_save_state`;
    never write `state[uid] = sess` (that would clobber the user record)."""
    rec = _dorossi_user_record(state, uid)
    sessions = rec["sessions"]
    active = rec.get("active")
    if active not in sessions:
        sid = _dorossi_new_session(rec)
        return sid, rec["sessions"][sid]
    return active, sessions[active]


def _dorossi_session_by_id(state: dict, uid: str, sid: str) -> tuple[str, dict]:
    """Return (session_id, session_dict) for a SPECIFIC pre-resolved slot id. Used
    by the parallelised turn path, which resolves the target slot once at dispatch
    (under the state short-lock) and re-fetches it on each state read-modify-write
    so a fresh `state` reload doesn't carry a stale slot reference across a backend
    call. If the slot vanished (deleted between dispatch and now — should not
    happen while the turn holds the session lock, but guard anyway) we fall back to
    the active session so the turn still has a live slot to operate on. The
    returned dict is the live slot inside `state`; mutate it then
    `_dorossi_save_state` — never write `state[uid] = sess`."""
    rec = _dorossi_user_record(state, uid)
    sessions = rec["sessions"]
    if sid in sessions:
        return sid, sessions[sid]
    return _dorossi_active_session(state, uid)


def _dorossi_reset_session(sess: dict) -> None:
    """Drop the backend continuity (session id, scoped dirs, api history), the
    session-scoped tuning overrides (`tune_effort`/`tune_model` — a reset/new
    context starts from the defaults, owner ruling) AND any unfinished-loop
    marker (`loop_pending` — a reset context has nothing to resume) so the next
    turn starts fresh, keeping the slot's id/label/created_at. In place.
    `cc_usage_mark` (the cumulative-usage baseline of the dropped backend session,
    see `_dorossi_cc_account_round`) goes too: it is keyed to that session id and
    would never match again."""
    for k in ("cc_session_id", "codex_session_id", "cc_cwd", "cc_extra_dir", "api_history",
              "tune_effort", "tune_model", "loop_pending", "cc_usage_mark"):
        sess.pop(k, None)
    sess["last_used"] = time.time()


def _dorossi_mark_loop_pending(sess: dict, task: str, *,
                               channel_id=None, message_id=None) -> None:
    """Record "there is an unfinished self-loop task" on the session slot (in
    place; called when the self-loop starts, and cleared with
    _dorossi_clear_loop_pending on a clean finish). The task description is kept
    for a later "fresh rerun" and for the list display; when the new description is
    empty the old one is kept (resuming the same task does not wipe the original
    task text). No interruption path (abort / silence backstop / usage limit /
    exception / bot restart) clears this marker, so the owner can resume afterwards
    with `@bot session <id> continue`. Pure function, never raises.

    It also records three things for "automatic resumption across restarts":

    * `live` -- "a process is running this loop right now". The only place that
      sets it True is here; the only place that sets it back to False is
      `_dorossi_mark_loop_stopped`, which is **only called from the loop's
      `finally`**. `finally` always runs on a voluntary end (abort / silence /
      exception / giving up) and never runs when the process is killed -- so
      "seeing live still True after a restart" means exactly "the previous process
      was killed, it did not stop by itself". That is precisely the criterion
      automatic resumption needs: once the owner has pressed abort, the loop must
      not be brought back automatically.
    * `channel_id` / `message_id` -- the anchor used when resuming. After a restart
      the record is looked up from the platform and used as `_dorossi_run_loop`'s
      `message`, and **the owner gate is re-verified against the initiator the
      platform reports**, not by trusting the id stored here. ⚠️ For a slash command
      `message_id` is an **interaction id**, not a message id -- the bot side's
      `_resolve_trigger_message` finds the initiator again from the
      `interaction_metadata` of the bot's own reply (before 2026-09-19 it fetched
      the message directly, which always 404'd, so this feature had never once
      worked). If it cannot be looked up, there is no automatic resumption and the
      marker stays for a manual one.
    * `auto_tries` -- the count of consecutive automatic resumptions, the circuit
      breaker for a crash loop; the old value is carried over (resuming the same
      task does not reset it; it is reset only when a round really completes, see
      `_dorossi_touch_loop_pending`).
    """
    prev = sess.get("loop_pending")
    prev = prev if isinstance(prev, dict) else {}
    text = (task or "").strip() or prev.get("task", "")
    marker = {"ts": time.time(), "task": text[:2000], "live": True}
    for key, value in (("channel_id", channel_id), ("message_id", message_id)):
        keep = value if isinstance(value, int) and not isinstance(value, bool) \
            else prev.get(key)
        if isinstance(keep, int) and not isinstance(keep, bool):
            marker[key] = keep
    tries = prev.get("auto_tries")
    if isinstance(tries, int) and not isinstance(tries, bool) and tries > 0:
        marker["auto_tries"] = tries
    sess["loop_pending"] = marker


# Why the self-loop stopped (`loop_pending["stop"]`, written by the loop's
# `finally`). Only "the network dropped" and "cancelled" (the task was cancelled
# on bot shutdown / reconnect) count as **not a stop it chose**, and those are
# resumed **automatically**; abort and paused are never resumed automatically
# (both are intents the owner stated explicitly, which automation must not
# override -- the difference is that paused can still be brought back by hand
# with `/dorossi session continue`). Old markers have no such key: with `live`
# false it is treated as an unknown reason, and automatic resumption still does
# not resume it.
DOROSSI_LOOP_STOP_REASONS = frozenset({
    "abort",        # the owner's `/dorossi abort`
    "network",      # the platform connection dropped; the loop can no longer speak
    "interrupted",  # the task was cancelled (bot shutdown, reaped on reconnect)
    "silence",      # output-silence retries ran out
    "usage",        # usage waits reached the configured safety cap
    "transient",    # transient-failure retries ran out
    "error",        # any other error (including fatal ones)
    "deleted",      # slot deleted (the marker is gone with it, nothing to write; kept for completeness)
    "paused",       # the owner's `/dorossi yield`: commit, yield editing rights, pause until someone takes over
})
# Only these two count as "not a stop it chose" and are brought back
# **automatically** on reconnect / restart. `paused` is deliberately not
# included: the whole point of yielding is "another editor is changing the same
# set of files", and automatically calling the loop back on restart to touch that
# same set of files is exactly what it is meant to avoid -- so paused always waits
# for the owner's own `continue`.
_DOROSSI_AUTORESUMABLE_STOPS = frozenset({"network", "interrupted"})


def _dorossi_touch_loop_pending(sess: dict, *, reset_tries: bool = False) -> None:
    """Move the marker's heartbeat to now (in place). **Never creates a marker** --
    with no marker it does nothing, otherwise a slot that "finished naturally and
    had its marker cleared" would be revived by the heartbeat into "has an
    unfinished task".

    Why the heartbeat exists: if `ts` were written only once when the loop starts,
    a task that had been running for three days would be refused automatic
    resumption after a restart because "the marker is too old" -- exactly the case
    this feature should resume most. It is bumped once after each completed round
    (`reset_tries=True`, which also clears the crash-loop counter) and once before
    entering a usage wait, so `ts` means "the last moment this loop was proven to
    be alive". Never raises."""
    marker = sess.get("loop_pending")
    if not isinstance(marker, dict):
        return
    marker["ts"] = time.time()
    marker["live"] = True
    marker.pop("stop", None)   # still alive, so there is no "reason it stopped"
    if reset_tries:
        marker.pop("auto_tries", None)


def _dorossi_mark_loop_stopped(sess: dict, reason: str | None = None) -> None:
    """Mark "the loop stopped by itself, it was not killed" (in place). **Only
    called from the self-loop's `finally`**; for why, see the `live` note on
    `_dorossi_mark_loop_pending`. Never creates a marker (the natural-finish path
    has already cleared the whole marker; do not revive it). Never raises.

    `reason` (one of `DOROSSI_LOOP_STOP_REASONS`) is recorded in `stop`: `live`
    alone cannot tell "the owner pressed abort" from "the network dropped", and the
    latter is exactly what should be brought back (the 2026-09-22 network outage
    wrote all six tasks as `live=False`, so not one was brought back). An
    unrecognised value is not written -- no reason is the same as an old marker."""
    marker = sess.get("loop_pending")
    if isinstance(marker, dict):
        marker["live"] = False
        if reason in DOROSSI_LOOP_STOP_REASONS:
            marker["stop"] = reason
            marker["stopped_ts"] = time.time()
        else:
            marker.pop("stop", None)


def _dorossi_mark_loop_aborted(sess: dict) -> None:
    """The owner pressed abort on a task that is **not running but waiting to be
    resumed automatically** (in place).

    While the loop is running, abort is written as `stop: abort` by the loop's own
    `finally`; this one is for the kind where "the loop already stopped because the
    network dropped, and will be brought back once the connection returns" --
    without it the owner's abort would be useless, and the task would come back to
    life by itself as soon as the network returned. Never creates a marker. Never
    raises."""
    marker = sess.get("loop_pending")
    if isinstance(marker, dict):
        marker["live"] = False
        marker["stop"] = "abort"
        marker["stopped_ts"] = time.time()


def _dorossi_clear_loop_pending(sess: dict) -> None:
    """Clear the "unfinished self-loop task" marker (called when the self-loop
    finishes naturally -- stopping after consecutive rounds without progress)."""
    sess.pop("loop_pending", None)


def _dorossi_loop_marker_wants_autoresume(marker) -> bool:
    """The marker itself says "this task did not choose to stop": `live` is still
    True (the process was killed), or the reason it stopped is in
    `_DOROSSI_AUTORESUMABLE_STOPS`. Abort and old markers (no reason, `live` false)
    are always no. Pure function, never raises."""
    if not isinstance(marker, dict):
        return False
    if marker.get("live") is True:
        return True
    return marker.get("stop") in _DOROSSI_AUTORESUMABLE_STOPS


def _dorossi_loop_autoresume_plan(sess: dict, *, now=None):
    """Should the bot, on start-up, bring this slot's self-loop task back by
    itself? (pure function, never raises)

    Returns `(channel_id, message_id, tries)` or None. Non-None only when all four
    conditions pass:

    1. There is a well-formed `loop_pending` with `live` True -- i.e. the previous
       process was killed; **or** the loop stopped by itself but for a reason in
       `_DOROSSI_AUTORESUMABLE_STOPS` (the network dropped, the task was
       cancelled). The owner's own abort / the silence backstop / an exception /
       giving up are not among them.
    2. There are `channel_id` and `message_id` anchors (old markers lack these two
       keys, so they can only be resumed by hand -- deliberate backward-compatible
       behaviour, not a hole).
    3. The heartbeat is within `DOROSSI_LOOP_AUTORESUME_MAX_AGE_SEC` (0 = feature
       off). A "future heartbeat" caused by the clock going backwards is always
       treated as expired; a negative age does not get to sneak through.
    4. `auto_tries` has not reached `DOROSSI_LOOP_AUTORESUME_MAX_TRIES` (0 = no
       limit).

    **No permission check happens here.** The owner gate is always re-verified by
    the caller against "the message actually fetched back"; an id stored on disk is
    not grounds for authorisation."""
    max_age = DOROSSI_LOOP_AUTORESUME_MAX_AGE_SEC
    if not isinstance(max_age, (int, float)) or isinstance(max_age, bool) \
            or max_age <= 0 or max_age != max_age:  # NaN also counts as off
        return None
    marker = sess.get("loop_pending")
    if not _dorossi_loop_marker_wants_autoresume(marker):
        return None
    cid, mid = marker.get("channel_id"), marker.get("message_id")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0
               for v in (cid, mid)):
        return None
    ts = marker.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts != ts:
        return None
    age = (time.time() if now is None else now) - ts
    if not 0 <= age <= max_age:
        return None
    tries = marker.get("auto_tries")
    tries = tries if isinstance(tries, int) and not isinstance(tries, bool) \
        and tries > 0 else 0
    cap = DOROSSI_LOOP_AUTORESUME_MAX_TRIES
    if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 \
            and tries >= cap:
        return None
    return (cid, mid, tries)


def _dorossi_count_autoresume(sess: dict) -> None:
    """Add one to this slot's "consecutive automatic resumptions" (in place).
    Called **before actually starting**, so even if the resumption kills the bot
    again on the spot, the count has already landed -- exactly what the circuit
    breaker needs. Never creates a marker. Never raises."""
    marker = sess.get("loop_pending")
    if not isinstance(marker, dict):
        return
    tries = marker.get("auto_tries")
    tries = tries if isinstance(tries, int) and not isinstance(tries, bool) \
        and tries > 0 else 0
    marker["auto_tries"] = tries + 1


def _dorossi_loop_resume_plan(sess: dict):
    """Judge whether a session slot has "a resumable self-loop task", and how to
    resume it (pure function).

    Returns:
      * None -- no resumable task (no loop_pending marker, or the marker is
        malformed).
      * ("continue", None) -- the backend context is still there (there is a
        cc_session_id): resume the same session and carry on with the CONTINUE
        prompt, the most complete option (the self-loop already_ran_first=True
        path).
      * ("fresh", task) -- the context is gone (the session was cleared) but a task
        description remains: start over with the original task text (the
        already_ran_first=False path).
    Never raises; a store hand-edited into an odd shape is always treated as
    "nothing to resume"."""
    pending = sess.get("loop_pending")
    if not isinstance(pending, dict):
        return None
    if sess.get("cc_session_id") or sess.get("codex_session_id"):
        return ("continue", None)
    task = pending.get("task")
    if isinstance(task, str) and task.strip():
        return ("fresh", task.strip())
    return None


def _dorossi_session_is_stale(sess: dict) -> bool:
    """True when an existing session hasn't been used for longer than the
    configured max age (conservative single-turn hygiene). 0/disabled, a missing
    timestamp, or a clock skew (last_used in the future) → never stale. Bounds an
    unbounded long-lived session WITHOUT forcing the user to reset manually; the
    caller only acts when the slot actually has continuity to drop."""
    if DOROSSI_SESSION_MAX_AGE_DAYS <= 0:
        return False
    last = sess.get("last_used") or sess.get("created_at")
    if not isinstance(last, (int, float)) or isinstance(last, bool) or last <= 0:
        return False
    age_days = (time.time() - last) / 86400.0
    return age_days >= DOROSSI_SESSION_MAX_AGE_DAYS


def _dorossi_most_recent_session(sessions: dict) -> str | None:
    """Id of the most-recently-used remaining session (by last_used, then
    created_at), or None when there are none. Used to re-point `active` after a
    delete."""
    if not sessions:
        return None
    return max(
        sessions,
        key=lambda sid: (sessions[sid].get("last_used")
                         or sessions[sid].get("created_at") or 0))


def _dorossi_load_state() -> dict:
    """Load the per-user session store, migrated to the multi-session shape.
    Never raises — a missing/corrupt file just means 'no sessions yet'."""
    try:
        text = DOROSSI_SESSION_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except Exception as exc:  # pylint: disable=broad-except
        # A transient I/O problem (locked / permissions): touch nothing, and treat
        # this time as "no sessions yet".
        print(f"[dorossi] session load failed: {exc!r}", file=sys.stderr)
        return {}
    try:
        raw = _json.loads(text)
    except Exception as exc:  # pylint: disable=broad-except
        # Corrupt content (not a transient I/O problem): first move the bad file
        # aside to .bad to keep it, then return an empty state. Left as is, the
        # next _dorossi_save_state would overwrite the whole file with an "empty
        # state" (saving is a whole-file temp+os.replace), and every one of the
        # user's sessions would be gone for good, without even a chance of a
        # manual rescue. The move itself is fail-soft too; a failure only goes to
        # stderr.
        print(f"[dorossi] session file corrupt, quarantining: {exc!r}",
              file=sys.stderr)
        try:
            os.replace(DOROSSI_SESSION_FILE,
                       DOROSSI_SESSION_FILE.with_name(
                           DOROSSI_SESSION_FILE.name + ".bad"))
        except Exception as exc2:  # pylint: disable=broad-except
            print(f"[dorossi] session quarantine failed: {exc2!r}",
                  file=sys.stderr)
        return {}
    return _dorossi_migrate_state(raw)


def _dorossi_save_state(state: dict) -> None:
    """Persist the session store atomically (temp + os.replace)."""
    try:
        tmp = DOROSSI_SESSION_FILE.with_name(DOROSSI_SESSION_FILE.name + ".tmp")
        tmp.write_text(_json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, DOROSSI_SESSION_FILE)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] session save failed: {exc!r}", file=sys.stderr)


# Markers that identify a plan / quota usage-limit notice in the claude_code
# backend's `result` text (or its `result` event). The headless CLI reports a
# usage limit either as a non-zero exit OR as an rc==0 run whose `result` text
# is actually the limit notice rather than an answer — both are matched here.
# Kept lower-cased; the matcher lower-cases its input. Phrasings cover the
# documented "You've hit your … limit · resets …" line, the generic "usage
# limit reached", explicit rate-limit wording, and the pipe-delimited
# "Claude AI usage limit reached|<epoch>" variant seen in the wild.
_DOROSSI_USAGE_LIMIT_MARKERS = (
    "usage limit reached",
    "usage limit exceeded",
    "hit your usage limit",
    "you've hit your",          # "You've hit your session/weekly/Opus limit"
    "limit reached",
    "rate limit",
    "rate_limit",
    "5-hour limit",
    "five_hour",
    "weekly limit",
    "out of credits",
    "out_of_credits",
)

# The table above **may only be used to judge "error text"**, never "the answer
# of a successful round" (fixed 2026-09-19).
#
# The table matches very broadly ("rate limit", "limit reached", "five_hour"),
# while the result text of a successful round is **prose the model wrote
# itself**. At 2026-09-17 22:44 and 2026-09-19 05:42 there was one rc==0,
# subtype=success normal answer each that was judged a usage limit merely because
# the body **discussed** some SDK's rate limit (the latter was 3892 characters,
# with the hit at character 2298): the whole answer was thrown away, and the
# self-loop slept until the five-hour window's reset time, waiting an hour and
# forty-eight minutes for nothing.
#
# Real limit notices appeared 42 times in this machine's log (since 09-03), and
# **every one** was rc=1, is_error=True, api_error_status=429 -- the structured
# fields suffice to judge it and the text is not needed at all. The rc==0 text
# path is a line of defence kept for older upstream CLIs where "the result text
# itself is the notice", so on a **successful** result, text evidence counts
# only when both conditions hold (`_dorossi_cc_limit_text_counts`):
#
#   1. The stream's `rate_limit_event` did not say "this call was allowed". If the
#      server's quota headers say allowed, this round cannot be a quota refusal --
#      a structured veto.
#   2. The text **looks like a notice**: the CLI's notice is a one-line template
#      ("You've hit your session limit · resets 4:50pm (Asia/Taipei)", the
#      longest of those 42 was 65 characters), and the CLI also appends tails like
#      "· progress saved" and "· ask your admin for a higher limit", so the cap is
#      left at 300 characters; an answer that discusses rate limits is prose of
#      hundreds or thousands of characters.
#
# Each of the two blocks one case, **do not keep just one**: the veto only works
# when the event exists (older CLIs and API-key sessions have no such event), and
# the length cannot stop "a short answer that happens to mention rate limit".
# An error result (is_error true, subtype not success, or no result event at all)
# is judged by the whole table as before: the text then is the CLI's error
# message, not the model's answer.
_DOROSSI_USAGE_NOTICE_MAX_CHARS = 300
# The vocabulary of `rate_limit_event.rate_limit_info.status`. **Not guessed**:
# taken from the CLI's own SDK event schema (in the 2.1.276 executable,
# `status:q(["allowed","allowed_warning","rejected"])`, checked 2026-09-19), with
# the "allowed" received in a real run on 2026-08-31 as evidence
# (`test_dorossi_usage_limit.REAL_RATE_LIMIT_EVENT`). The first two mean this call
# was allowed (allowed_warning = close to the limit, but still allowed), and only
# those two can veto the text verdict.
_DOROSSI_RATE_STATUSES = frozenset({"allowed", "allowed_warning", "rejected"})
_DOROSSI_RATE_STATUS_ALLOWED = frozenset({"allowed", "allowed_warning"})


# `reset_hint` is the **only** raw backend text this module lets into a Discord
# reply (_dorossi_usage_limit_reply posts it as "usage expected to reset at
# <hint>"), so it must be treated as untrusted input. The danger is that
# _DOROSSI_USAGE_LIMIT_MARKERS matches very broadly ("rate limit", "limit
# reached" …), so backend text that is really a different error and merely happens
# to contain "reset" also ends up here, e.g. "rate limit… connection reset by peer
# while writing D:\\…\\x.log" -- returned verbatim it would send a host path /
# service name / raw exception into Discord, violating the hard requirement of "no
# service names / local paths / raw errors". So only the known-safe shape "reset(s)
# + a short time phrase" is accepted, and everything else is dropped (returns
# None, and the caller replies with just the generic usage-limit notice; the
# feature is unaffected).
# The character set deliberately excludes `/`, `\\`, `~`, quotes, backticks and
# other characters common in paths / URLs.
_DOROSSI_RESET_HINT_MAX = 40
_DOROSSI_RESET_HINT_RE = re.compile(r"^resets?\b[A-Za-z0-9 :,.+-]*",
                                    re.IGNORECASE)


def _dorossi_sanitize_reset_hint(snippet: str) -> str | None:
    """Narrow a "reset time" fragment extracted from backend text down to a
    known-safe shape, otherwise return None. Pure function, never raises (see the
    comment above for the secrecy reason).

    It works by **taking the longest prefix of allow-listed characters**, not by
    "accept only if the whole thing matches". The original all-or-nothing version
    threw away upstream's most common sentence entirely -- the parentheses in
    "Your limit will reset at 1pm (Etc/GMT+5)" are not in the character set, so the
    owner saw no time at all. The prefix version is just as safe (the prefix lies
    entirely within the allow-list and cannot smuggle in a path or URL); it just
    does not give up a whole sentence over one parenthesis.

    After truncation there are two more checks:

    * `[A-Za-z]:` -- a letter immediately followed by a colon means a drive prefix
      (`D:`) or a scheme (`http:`). In a time the colon is always preceded by a
      digit. This check is the main killer of fake hints like `rate limit…
      connection reset by peer while writing D:\\…`.
    * **Must contain a digit** -- a "reset time" always has a digit. Without this,
      prose with no drive prefix such as `reset by peer while writing logs` would
      be sent out verbatim as a time. Truncation makes such sentences more likely
      to be "entirely legal by chance", so this check is its counterpart, not extra
      fastidiousness.
    """
    s = (snippet or "").strip()
    if not s:
        return None
    match = _DOROSSI_RESET_HINT_RE.match(s)
    if match is None:
        return None
    s = match.group(0).strip()
    if not s or len(s) > _DOROSSI_RESET_HINT_MAX:
        return None
    # In a time the colon is always preceded by a digit (3:45pm); letter + colon
    # means a drive prefix (D:) or a scheme (http:), always rejected.
    if re.search(r"[A-Za-z]:", s):
        return None
    if not any(ch.isdigit() for ch in s):
        return None
    return s


# The pipe-delimited timestamp variant: `Claude AI usage limit reached|1749924000`.
# In practice this is the only **machine-readable** source of the reset time
# (upstream issue titles show this shape a lot), so the self-loop's "sleep until
# the quota returns" trusts only this one. 10 digits = seconds, 13 digits =
# milliseconds; both are accepted.
_DOROSSI_RESET_EPOCH_RE = re.compile(r"\|\s*(\d{10,13})\b")
# The plausible epoch range (seconds): 2001-09 to 2096-10. Anything outside is
# taken as "that string of digits is not a timestamp".
_DOROSSI_EPOCH_MIN = 1_000_000_000
_DOROSSI_EPOCH_MAX = 4_000_000_000


def _dorossi_extract_reset_epoch(text: str) -> float | None:
    """Extract the **machine-readable** reset time (epoch seconds) from a
    usage-limit notice, or None when there is none. Pure function, never raises.

    **Only the `…|<epoch>` shape is recognised; a human-readable "resets 3:45pm" is
    deliberately not converted.** The reason is the time zone: what upstream
    actually prints is "Your limit will reset at 1pm (Etc/GMT+5)", a time zone that
    has nothing to do with the local one, so converting `1pm` as local time could
    be off by several whole hours. The two directions of a wrong guess cost
    differently -- guessing early only sends one more probe that fails at once
    (cheap), while guessing late stalls the whole self-loop task for hours for
    nothing (expensive). So without a timestamp it always returns None and lets
    the caller probe with "a short initial wait, exponential backoff", rather than
    trusting a clock time with no time zone.
    `reset_hint` (the string for people) is unaffected and still shows the clock
    time as written.
    """
    if not text:
        return None
    try:
        match = _DOROSSI_RESET_EPOCH_RE.search(text)
        if match is None:
            return None
        raw = int(match.group(1))
        # 13 digits is milliseconds (defensive: measured as seconds today, but
        # accepting both keeps it from quietly going wrong some day).
        secs = raw / 1000.0 if raw >= 1_000_000_000_000 else float(raw)
        if _DOROSSI_EPOCH_MIN <= secs <= _DOROSSI_EPOCH_MAX:
            return secs
    except Exception:  # pylint: disable=broad-except
        return None
    return None


def _dorossi_rate_limit_reset(ev: dict) -> float | None:
    """Extract the reset time (epoch seconds) from stream-json's
    `rate_limit_event`. Pure function, never raises.

    **This is currently the only truly reliable machine-readable source.** Originally
    only `…|<epoch>` in the notice text was recognised -- a shape that came from
    upstream issue titles, and measured on 2026-08-31 **the current CLI no longer
    prints it at all** (`Claude AI usage limit reached` appears nowhere in the whole
    executable). So that path effectively always returned None, and every limit hit
    could only take the "start at 15 minutes, double each time" backoff probe,
    waiting hours for nothing in the worst case.

    The current CLI instead emits a `rate_limit_event` in the stream on **every
    call**:

        {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "resetsAt": 1788199200,
            "rateLimitType": "five_hour",
            "unifiedWindows": {"five_hour": {"utilization": 0.49,
                                             "resetsAt": 1788199200},
                               "seven_day": {...}}}}

    `resetsAt` is this window's real reset time (from the server's quota headers),
    so on a limit hit it can **sleep until that moment** instead of guessing. The
    top-level `resetsAt` is preferred -- the CLI has already picked, by
    `rateLimitType`, the window that is binding right now; only when that is
    missing does it fall back to `unifiedWindows.five_hour`.

    A weekly limit's (`rateLimitType == "seven_day"`) `resetsAt` may be days away,
    but the caller's `DOROSSI_USAGE_WAIT_MAX_SEC` caps a single wait at 6 hours and
    probes again -- probing is cheap, oversleeping is the irreversible waste. That
    judgement is not made here; this only reports the time faithfully.
    """
    try:
        info = ev.get("rate_limit_info")
        if not isinstance(info, dict):
            return None
        candidates = [info.get("resetsAt")]
        windows = info.get("unifiedWindows")
        if isinstance(windows, dict):
            for name in ("five_hour", "seven_day"):
                win = windows.get(name)
                if isinstance(win, dict):
                    candidates.append(win.get("resetsAt"))
        for raw in candidates:
            if not isinstance(raw, (int, float)) or isinstance(raw, bool):
                continue
            secs = float(raw)
            if secs != secs:  # NaN
                continue
            if secs >= 1_000_000_000_000:  # milliseconds (defensive; measured as seconds)
                secs /= 1000.0
            if _DOROSSI_EPOCH_MIN <= secs <= _DOROSSI_EPOCH_MAX:
                return secs
    except Exception:  # pylint: disable=broad-except
        return None
    return None


def _dorossi_rate_limit_status(ev) -> str | None:
    """The `rate_limit_info.status` of a `rate_limit_event`; None when it is not in
    the known vocabulary. Pure function, never raises.

    An unrecognised value (the vocabulary grows some day, the field changes shape)
    always returns None, **not** treated as allowed: the only use of this value is
    to veto the text verdict (see `_dorossi_cc_limit_text_counts`), so the failure
    direction for an unrecognised value must be "no veto" -- fall back to the
    original text verdict rather than let a real notice through.
    """
    try:
        info = ev.get("rate_limit_info")
        if not isinstance(info, dict):
            return None
        status = info.get("status")
        if isinstance(status, str) and status in _DOROSSI_RATE_STATUSES:
            return status
    except Exception:  # pylint: disable=broad-except
        return None
    return None


def _dorossi_cc_limit_text_counts(result_ev: dict, answer: str,
                                  stream_status: str | None) -> bool:
    """Whether a **text** hit against the usage-limit table counts this time. Pure
    function.

    The rules are written above `_DOROSSI_USAGE_NOTICE_MAX_CHARS`; this does only
    three things:

    * The result did not complete successfully (`_claude_result_succeeded` false)
      -> counts. The text then is the CLI's error message, judged by the whole
      table as before, so rc != 0 behaviour has not changed by a single word.
    * Completed successfully, and the stream says this call was allowed -> does
      not count (structured veto).
    * Completed successfully, with no allowed signal -> the text counts only when
      it is short enough to be a notice.

    The length measured is the **longer** of `result` and `answer`: in practice
    they are the same text, but if either is long-form prose, this is not a notice.
    """
    if not _claude_result_succeeded(result_ev):
        return True
    if stream_status in _DOROSSI_RATE_STATUS_ALLOWED:
        return False
    raw = result_ev.get("result")
    texts = (raw if isinstance(raw, str) else "", answer if isinstance(answer, str) else "")
    return max(len(t.strip()) for t in texts) <= _DOROSSI_USAGE_NOTICE_MAX_CHARS


def _dorossi_extract_reset_hint(text: str) -> str | None:
    """Pull a reset time out of a usage-limit notice, if present. Handles the
    pipe-delimited `…|<epoch-seconds>` variant (rendered as a local time) and
    the human-readable `resets <when>` phrasing. Returns None when nothing
    parseable is found. Never raises."""
    if not text:
        return None
    try:
        # Variant: "Claude AI usage limit reached|1717689600"
        epoch = _dorossi_extract_reset_epoch(text)
        if epoch is not None:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
        # Variant: "… · resets 3:45pm" / "… resets Mon 12:00am"
        low = text.lower()
        idx = low.find("reset")
        if idx != -1:
            snippet = text[idx:idx + 80].strip()
            # Trim at a sentence/line boundary so the hint stays short.
            for sep in ("\n", ". ", "。"):
                cut = snippet.find(sep)
                if cut > 0:
                    snippet = snippet[:cut]
            # This is raw backend text that gets posted into Discord -> narrow it
            # to a known-safe shape first.
            return _dorossi_sanitize_reset_hint(snippet)
    except Exception:  # pylint: disable=broad-except
        return None
    return None


def _dorossi_cc_usage_limit(result_ev: dict, answer: str,
                            session_id: str | None = None,
                            *, stream_reset: float | None = None,
                            stream_status: str | None = None
                            ) -> _DorossiUsageLimitError | None:
    """Inspect a claude_code `result` event (+ its answer text) and return a
    populated _DorossiUsageLimitError when it represents a plan/quota usage
    limit, else None. Checks the structured `api_error_status` (429 / 402) and
    scans `subtype` + the result/answer text for the documented limit markers,
    so it catches BOTH the non-zero-exit failure and the rc==0 run whose answer
    text is itself the limit notice.

    **Structured fields always take precedence over text**: 429 / 402 go through
    no text condition at all. A text hit must additionally pass
    `_dorossi_cc_limit_text_counts` -- in a successfully completed round the text
    is the answer the model wrote, not the CLI's notice (the 2026-09-19 incident,
    see above `_DOROSSI_USAGE_NOTICE_MAX_CHARS`).
    `stream_status` is the status of this call's last `rate_limit_event`."""
    status = result_ev.get("api_error_status")
    try:
        status_int = int(status) if status not in (None, "") else None
    except (TypeError, ValueError):
        status_int = None
    haystack = " ".join(
        str(result_ev.get(k) or "")
        for k in ("subtype", "result", "terminal_reason")
    )
    if answer:
        haystack += " " + answer
    low = haystack.lower()
    text_hit = (any(marker in low for marker in _DOROSSI_USAGE_LIMIT_MARKERS)
                and _dorossi_cc_limit_text_counts(result_ev, answer, stream_status))
    if status_int in (429, 402) or text_hit:
        reset_text = result_ev.get("result") or answer or ""
        reset = _dorossi_extract_reset_hint(reset_text)
        # The epoch scans **the whole haystack** (including subtype /
        # terminal_reason), because `…|<epoch>` is not guaranteed to appear in the
        # `result` field; while `reset_hint` keeps looking only at result/answer,
        # since that path gets posted into Discord verbatim and the narrower its
        # scan the better.
        #
        # **The stream event takes precedence over the text.** `stream_reset`
        # comes from this call's `rate_limit_event` (the server's quota headers)
        # and is structured; the `|<epoch>` in text comes from that upstream
        # issue-title shape, which measured on 2026-08-31 the current CLI no longer
        # prints. Only when neither is available does it return None, leaving the
        # caller to probe with backoff.
        epoch = stream_reset
        if epoch is None:
            epoch = _dorossi_extract_reset_epoch(haystack)
        # No human-readable clock time but a machine-readable moment -> build a
        # string for people from the moment, otherwise the owner is told "will
        # resume automatically" with no idea of roughly when.
        if reset is None and epoch is not None:
            reset = time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
        return _DorossiUsageLimitError(
            haystack.strip()[:300], reset,
            reset_at=epoch,
            session_id=session_id or None)
    return None


# ---- usage limit / transient failure on the codex (GPT) side ----------------
#
# Added 2026-09-05. Before this **the codex path had neither verdict at all**:
# rc!=0 in `_dorossi_via_codex` would only go "has session_id ->
# `_DorossiResumeError` (throw the session away and reopen once) -> otherwise
# `RuntimeError`", so when GPT hit a usage limit the loop first reopened a new
# session for nothing (which hit it just the same), then declared the whole
# unattended task dead. The Claude side had changed to "wait for the quota to
# recover, then carry on" back on 2026-08-31; the codex side kept the old
# behaviour.
#
# `_DorossiUsageLimitError` / `_DorossiTransientError` are deliberately **reused**
# here: the self-loop already knows how to wait on these two exceptions, so as
# long as the codex path raises the right one, not a single line of the waiting
# and resuming logic needs to change.

# Relative-time phrasing ("try again in 2.363s", "try again in 4 days 2 hours 46
# minutes"). Grab a small window on **the same line** after `… in `, and let the
# unit match below pick from it.
# The first version used a narrowed character set `[0-9smhd\s.,]*`, which looked
# safe but in fact stopped at the `a` of "4 days" -- "4 days 2 hours 46 minutes"
# counted only 4 days, and the 2 hours 46 minutes **silently vanished**. The
# window version is just as safe (the unit match requires "number + time-unit
# word", so the message's "Limit 200000, Used 162582" has no unit and is not
# caught by mistake), but does not miss the spelled-out forms.
_RETRY_AFTER_RE = re.compile(
    r"(?:try\s+again|retry|resets?)\s+(?:again\s+)?in\s+([^\n]{0,60})",
    re.IGNORECASE)
# The previous pattern cuts the input at 60 characters, so it is not slow today;
# but on its own it is quadratic in a long run of digits (291 seconds for 60,000
# digits), and the lookbehind makes it start only at the beginning of a digit
# run, with the same result.
_DURATION_UNIT_RE = re.compile(
    r"(?<![0-9])([0-9]+(?:\.[0-9]+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.IGNORECASE)
_UNIT_SECONDS = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}


def _dorossi_extract_retry_after_seconds(text: str) -> float | None:
    """Extract the seconds from a **relative** retry hint; None when there are
    none. Pure function, never raises.

    The opposite trade-off from `_dorossi_extract_reset_epoch`, and for the same
    reason: that one refuses to convert "resets 3:45pm" because there is **no time
    zone**, and a wrong guess could be off by several whole hours. The relative
    form has no such problem -- "in 4 days 2 hours" is the same length in any time
    zone, so converting it is safe. codex's limit notices use exactly the relative
    form.

    Only the stretch right after `try again in …` / `retry in …` / `resets in …` is
    recognised, rather than scanning the whole sentence for anything that looks
    like a time -- the message often carries other numbers (Limit 200000, Used
    162582), and grabbing them would produce absurd wait lengths.
    """
    if not text:
        return None
    match = _RETRY_AFTER_RE.search(str(text))
    if not match:
        return None
    total = 0.0
    for value, unit in _DURATION_UNIT_RE.findall(match.group(1)):
        try:
            total += float(value) * _UNIT_SECONDS[unit[0].lower()]
        except (ValueError, KeyError):
            continue
    if total <= 0:
        return None
    # Clamp the upper end: upstream occasionally spits out an absurd length, and
    # this value becomes the sleep's seconds directly.
    return min(total, 7 * 86400.0)


# Usage / quota (must wait for the quota to recover). Deliberately excludes
# "overloaded" and "server error" -- those are the transient failures below, and
# the two have different waiting strategies.
_CODEX_USAGE_MARKERS = (
    "usage limit",
    "rate limit",
    "rate_limit",
    "429",
    "too many requests",
    "quota",
    "insufficient_quota",
    "you've hit your",
    "weekly limit",
    "5h limit",
)

# Server-side transient failures (retrying with backoff is enough; no need to
# wait for the quota).
_CODEX_TRANSIENT_MARKERS = (
    "overloaded",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "temporarily unavailable",
    "500",
    "502",
    "503",
    "504",
    "529",
)


def _dorossi_codex_usage_limit(text: str, session_id: str | None = None
                               ) -> "_DorossiUsageLimitError | None":
    """Whether codex's output looks like "a plan / quota usage limit"; if so return
    a filled-in exception, otherwise None.

    `reset_at` is given a value only when it can be computed from a **relative**
    form (see `_dorossi_extract_retry_after_seconds`); otherwise it stays None and
    the loop probes with "a short initial wait, exponential backoff" -- the same
    strategy as the Claude side.

    **The Claude side's 2026-09-19 misjudgement (a successful answer thrown away
    for discussing rate limits) cannot happen here, for structural reasons**: this
    is called only from the rc != 0 branch of `_codex_stream_verdict` (rc == 0 is
    always straight "ok"), and the text fed in is stderr + failure events, never
    the `agent_message` answer text (`_CodexStreamState.feed`). If it is ever to
    check on rc == 0 as well, the `_dorossi_cc_limit_text_counts` rule must be
    applied first.
    """
    if not text:
        return None
    low = str(text).lower()
    if not any(marker in low for marker in _CODEX_USAGE_MARKERS):
        return None
    delta = _dorossi_extract_retry_after_seconds(text)
    reset_at = (time.time() + delta) if delta else None
    return _DorossiUsageLimitError(
        str(text)[:400],
        _dorossi_sanitize_reset_hint(str(text)),
        reset_at=reset_at,
        session_id=session_id or None)


def _dorossi_codex_transient(text: str, session_id: str | None = None
                             ) -> "_DorossiTransientError | None":
    """Whether codex's output looks like "the server is temporarily busy". **Must
    come after the usage-limit verdict** -- for the same reason as the Claude side:
    a usage limit has a reset time to wait for, far more precise than exponential
    backoff."""
    if not text:
        return None
    low = str(text).lower()
    if any(marker in low for marker in _CODEX_USAGE_MARKERS):
        return None          # the usage limit takes precedence; not caught here
    if not any(marker in low for marker in _CODEX_TRANSIENT_MARKERS):
        return None
    return _DorossiTransientError(str(text)[:400], session_id=session_id or None)


# ---- "does retrying this error have any chance of succeeding" --------------
#
# Added 2026-09-05. The self-loop used to have only three kinds of error it would
# carry on through (usage limit, transient failure, and the one added only on
# 2026-09-03); **any** other exception meant "post an error line, `return`, the
# whole unattended task ends". For a long task nobody is watching, that means any
# single sporadic failure -- the backend process killed by the system, one
# network blip, one unexpected exception -- leaves it standing there all night.
#
# The criterion is the same as `_supervisor.child_exit_is_fatal`: **"does
# retrying have any chance of succeeding"**, not "how severe is the error". A
# wrong setting or expired credentials will not succeed on retry either, but
# their exception types cannot be told apart from sporadic failures, so only the
# retry cap catches them.
_FATAL_ERROR_TYPES = (
    FileNotFoundError,      # the backend CLI is not on PATH -- retry a hundred times and it still is not
    NotADirectoryError,
    PermissionError,        # a permission problem does not fix itself
    ImportError,
    _DorossiCliOptionError,  # the CLI does not recognise a flag we pass -- it will not update itself between retries
    _DorossiAuthError,       # the CLI has no usable sign-in -- someone must sign in on the host; retrying won't fix it
)

# These phrases mean "the configuration / environment itself is wrong", so
# retrying is equally useless.
#
# **The CLI's two real not-logged-in texts are deliberately not added** ("Not
# logged in · Please run /login", "Failed to authenticate. API Error: 401 …",
# measured 2026-09-19): that class is instead judged by `_dorossi_cc_auth_failure`
# from structured fields, with text only as a fallback and length-capped, raising
# the typed `_DorossiAuthError` (in the type table above). Adding phrases here
# would mean an uncapped substring match against **any** exception message --
# and the message of `RuntimeError("claude -p exited …: result=<answer>")` holds
# the result text, so a long answer that happens to discuss /login would declare
# the self-loop dead. This month the usage-limit verdict already fell twice on
# exactly this pattern (above `_DOROSSI_USAGE_NOTICE_MAX_CHARS`).
_FATAL_ERROR_MARKERS = (
    "not found on path",
    "cli not found",
    "no such file or directory",
    "not authenticated",
    "invalid api key",
    "authentication",
    "permission denied",
)


# ---- Connectivity probe: is the network back? --------------------------------
#
# Waiting out a network outage does not guess a number of seconds; it **asks the
# network**: one DNS resolution + TCP connection to that backend's (or the chat
# platform's) host, and success means it is back. The host names are used only
# by this and are never sent into the chat platform.
DOROSSI_NETWORK_PROBE_HOSTS = {
    "claude_code": ("api.anthropic.com", 443),
    "api": ("api.anthropic.com", 443),
    "codex": ("chatgpt.com", 443),
    "platform": ("discord.com", 443),
}
DOROSSI_NETWORK_PROBE_TIMEOUT_SEC = 8.0


async def dorossi_network_reachable(target: str) -> bool:
    """Can `target` (a key of `DOROSSI_NETWORK_PROBE_HOSTS`) be reached right now?
    Never raises.

    It only does "resolve + connect then close", sending no data; the whole thing
    is bounded, so a hung resolver cannot hang the wait itself. An unrecognised
    target falls back to the platform host."""
    host, port = DOROSSI_NETWORK_PROBE_HOSTS.get(
        target, DOROSSI_NETWORK_PROBE_HOSTS["platform"])
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), DOROSSI_NETWORK_PROBE_TIMEOUT_SEC)
    except (OSError, asyncio.TimeoutError, TimeoutError, ValueError):
        return False
    except Exception:  # pylint: disable=broad-except
        return False
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 2.0)
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    return True


def dorossi_error_is_fatal(exc: BaseException) -> bool:
    """True = retrying is pointless; stop and let a human deal with it. Never
    raises.

    The conservative direction is deliberately "retryable": misjudged as fatal ->
    an unattended task stalls all night for nothing; misjudged as retryable -> at
    most a few more tries, then it stops anyway (the retry count is capped). The
    two mistakes cost very different amounts.
    """
    try:
        if isinstance(exc, _FATAL_ERROR_TYPES):
            return True
        text = f"{type(exc).__name__}: {exc}".lower()
        return any(marker in text for marker in _FATAL_ERROR_MARKERS)
    except Exception:  # pylint: disable=broad-except
        return False


def dorossi_error_retry_wait_seconds(attempt: int, *, base: float = 20.0,
                                     cap: float = 300.0) -> float:
    """How many seconds to wait before the `attempt`-th (counting from 1) retry of
    an unexpected error. Exponential backoff, capped at 5 minutes.

    Shorter than the transient-failure one (which starts at 30 seconds and caps at
    15 minutes): an overloaded server needs time to recover, while this is mostly a
    sporadic local failure, and waiting too long only wastes unattended time.
    """
    try:
        n = max(1, int(attempt))
    except (TypeError, ValueError):
        n = 1
    return float(min(cap, base * (2 ** (n - 1))))


def dorossi_abandoned_loops(state: dict, *, now: float | None = None) -> list:
    """Self-loop tasks that were interrupted and **will not be resumed
    automatically any more**. Returns `[(uid, sid, age_sec), …]`.

    Added 2026-09-05. `_dorossi_loop_autoresume_plan` has four conditions and
    returns None if any one fails -- marker too old, retries used up, channel
    anchor missing, or not live. In the first three cases the marker still sits at
    `live: True`, but **nothing will ever pick it up again**, and nothing anywhere
    says so. For a long unattended task, that amounts to "it actually stopped long
    ago, and you think it is still running".

    Only the ones that "should be brought back but cannot be" are reported here
    (`live` still true, or stopped because of the network / cancellation, see
    `_dorossi_loop_marker_wants_autoresume`); tasks the owner aborted or that
    stopped voluntarily in other ways are not anomalies and are not listed.
    """
    out = []
    if not isinstance(state, dict):
        return out
    for uid, rec in state.items():
        if not isinstance(rec, dict):
            continue
        for sid, sess in (rec.get("sessions") or {}).items():
            if not isinstance(sess, dict):
                continue
            marker = sess.get("loop_pending")
            if not _dorossi_loop_marker_wants_autoresume(marker):
                continue
            if _dorossi_loop_autoresume_plan(sess, now=now) is not None:
                continue          # can still be brought back, so not abandoned
            ts = marker.get("ts")
            age = ((now if now is not None else time.time()) - float(ts)
                   if isinstance(ts, (int, float)) and not isinstance(ts, bool)
                   else 0.0)
            out.append((str(uid), str(sid), max(0.0, age)))
    out.sort(key=lambda row: row[2], reverse=True)
    return out


def _dorossi_usage_wait_seconds(exc: Exception, attempt: int,
                                *, now: float | None = None) -> float:
    """After the self-loop hits the plan's usage limit, how long (seconds) to sleep
    this time before carrying on.

    `attempt` counts from 1 and is "which try within this **consecutive** stretch
    of waiting" -- the caller resets it as soon as any round in between succeeds.

    Two paths:

    1. `exc.reset_at` has a value (the backend gave `…|<epoch>` or the API's
       `retry-after`) -> sleep until that moment plus a
       `DOROSSI_USAGE_WAIT_GRACE_SEC` buffer.
    2. No value -> a **backoff probe** starting at `DOROSSI_USAGE_WAIT_FALLBACK_SEC`
       and doubling each time. It does not guess a clock time with no time zone
       like "resets 3:45pm" (see `_dorossi_extract_reset_epoch`).

    Both paths' results are clamped into `[DOROSSI_USAGE_WAIT_MIN_SEC,
    DOROSSI_USAGE_WAIT_MAX_SEC]`: the floor stops a hot loop when something is
    misjudged as a usage limit, and the ceiling makes sure that even if the backend
    reports an absurd future moment, it sleeps at most max before probing again.

    Pure function (`now` can be injected), never raises.
    """
    now = time.time() if now is None else now
    reset_at = getattr(exc, "reset_at", None)
    if isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool):
        remain = float(reset_at) - float(now) + DOROSSI_USAGE_WAIT_GRACE_SEC
        # The timestamp is already past (clock skew / the backend reported an old
        # window) -> this is not "no need to wait" but "this timestamp is
        # worthless"; fall back to the backoff probe rather than retrying at once.
        if remain > 0:
            return _dorossi_clamp_usage_wait(remain)
    try:
        shift = min(max(0, int(attempt) - 1), DOROSSI_USAGE_WAIT_MAX_SHIFT)
    except (TypeError, ValueError):
        # "Never raises" is this function's contract, and its whole path runs inside
        # "something already went wrong" handling; when attempt is passed as
        # something odd, fall back to the first wait length rather than blowing up
        # the usage-limit handling itself.
        shift = 0
    return _dorossi_clamp_usage_wait(
        DOROSSI_USAGE_WAIT_FALLBACK_SEC * (2 ** shift))


def _dorossi_clamp_usage_wait(secs: float) -> float:
    """Clamp the wait seconds into `[MIN, MAX]`. The ceiling itself is also clamped
    to be no less than the floor, so someone setting max to 10 seconds in the
    config file cannot end up switching off the idle-spin protection.

    **`nan` must be stopped by an explicit gate; `min` / `max` cannot stop it.**
    Every comparison with nan returns False, and CPython's `max(a, b)` means "take
    a first, then check `b > a`" -- so the result depends entirely on argument
    order: `max(MIN, nan)` returns MIN (nan is dropped), `max(nan, MIN)` returns nan
    (passed all the way down). This used to be written exactly the latter way, so
    measured on 2026-09-08, nan in gave nan out and the clamp effectively did not
    exist. The same shape is already recorded at `_gui_control.parse_duration` and
    `_batch_config._is_finite_number`; this is the third time -- so it is written
    as an explicit gate rather than relying on argument order: the correctness of
    the order is invisible, and the next person to reorder it would see no symptom.

    Deliberately **not** written as `isfinite`: the current behaviour for `inf` and
    `-inf` is right and meaningful -- `inf` clamps to the ceiling ("even if the
    backend reports an absurd future moment, sleep at most max before probing
    again" is exactly what the ceiling is for), and `-inf` clamps to MIN. Only nan
    carries "no information at all", with no meaningful clamp result, so it is
    handled on its own.

    nan returns **MIN**, not MAX: this function's floor is "hot-loop protection
    when something is misjudged as a usage limit", and returning MIN means "wait
    the shortest stretch and probe again"; returning MAX would let a meaningless
    number stop the unattended self-loop for 6 hours, the more expensive wrong
    direction.

    nan cannot get in today (upstream in `_dorossi_usage_wait_seconds`,
    `if remain > 0` is False for nan and falls to the backoff path) -- but that is
    **accidental protection**: that check was not written for nan, and
    `json.loads` accepts `NaN` by default, so the source really can produce nan.
    The contract is held here, at home.
    """
    value = float(secs)          # convert before the nan check, keeping numeric strings / Decimal accepted
    if math.isnan(value):
        return DOROSSI_USAGE_WAIT_MIN_SEC
    ceiling = max(float(DOROSSI_USAGE_WAIT_MAX_SEC), DOROSSI_USAGE_WAIT_MIN_SEC)
    return min(max(value, DOROSSI_USAGE_WAIT_MIN_SEC), ceiling)


def _dorossi_cc_budget_exceeded(result_ev: dict) -> bool:
    """True when a claude_code `result` event indicates our per-invocation
    --max-budget-usd cap was hit. The CLI signals this with
    subtype == "error_max_budget_usd" (and an `errors` entry like "Reached
    maximum budget ($X)"); the run exits non-zero with NO `result` answer. This
    is OUR own spend cap, NOT a stale session and NOT a plan/quota usage limit,
    so the caller must treat it gracefully (no resume retry, no exception)."""
    if not isinstance(result_ev, dict):
        return False
    if result_ev.get("subtype") == "error_max_budget_usd":
        return True
    errs = result_ev.get("errors")
    if isinstance(errs, list):
        return any("maximum budget" in str(e).lower() for e in errs)
    return False


def _dorossi_count(value) -> int | None:
    """A token-count field -> a non-negative integer; None when it is not a usable
    number. Never raises.

    `json.loads` accepts `NaN` / `Infinity` by default, and `int(float("nan"))`
    raises ValueError, `int(float("inf"))` raises OverflowError -- these three usage
    parsers all run after "the answer is already in hand" (the return line of
    `_dorossi_via_claude_code`), and raising there would turn a finished round into
    a failure. bool is a subclass of int and must be excluded too. Negative numbers
    are treated as bad values (a token count is never negative)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value)


def _dorossi_usage_int(usage: dict, keys) -> int:
    """Sum the named integer fields out of a claude_code `result.usage` block,
    defensively: missing / non-numeric / bool / non-finite / negative values
    contribute 0. Returns a plain int (>= 0). Never raises."""
    total = 0
    if not isinstance(usage, dict):
        return 0
    for k in keys:
        v = _dorossi_count(usage.get(k))
        if v is not None:
            total += v
    return total


def _dorossi_model_usage_totals(result_ev: dict) -> dict | None:
    """Sum the per-model usage in `result.modelUsage` into `{"in","cr","cc","out"}`.

    Why it is needed: the top-level `usage` **counts only the main model**, while
    `total_cost_usd` is the total across **all** models. On 2026-08-30 one ordinary
    Q&A round measured with the real CLI had a top-level `usage.input_tokens` of 2,
    while `modelUsage` held, besides the main model, a small model that consumed
    897 input / 9 output and spent $0.000942, and `total_cost_usd` counted both. In
    other words, reading only the top-level `usage` means the tokens and the money
    never reconcile, and the whole reason this record exists is to compare the two.

    The key names are camelCase (`inputTokens` / `cacheReadInputTokens` /
    `cacheCreationInputTokens` / `outputTokens`), unlike the top-level `usage`'s
    snake_case -- so `_dorossi_usage_int` cannot be shared. If the block is absent
    or not a dict it returns None, letting the caller fall back to the top-level
    `usage`. Never raises.
    """
    models = result_ev.get("modelUsage") if isinstance(result_ev, dict) else None
    if not isinstance(models, dict) or not models:
        return None
    totals = {"in": 0, "cr": 0, "cc": 0, "out": 0}
    keys = {"in": "inputTokens", "cr": "cacheReadInputTokens",
            "cc": "cacheCreationInputTokens", "out": "outputTokens"}
    seen = False
    for entry in models.values():
        if not isinstance(entry, dict):
            continue
        seen = True
        for slot, key in keys.items():
            value = _dorossi_count(entry.get(key))
            if value is not None:
                totals[slot] += value
    return totals if seen else None


_CONTEXT_KEYS = ("input_tokens", "cache_read_input_tokens",
                 "cache_creation_input_tokens")


def _dorossi_last_call_context(result_ev) -> int | None:
    """The context size of this invocation's **last** API call (in + cache_read +
    cache_creation).

    The source is the last entry of `result.usage.iterations`. Measured once each on
    CLI 2.1.276 and 2.1.277 on 2026-09-19 (4 API calls in one invocation): the
    top-level `usage` is **the sum over every call in this invocation** (cr 22,904 ≈
    4 × 7.8k), `modelUsage` adds the other models on top, while `iterations` **has
    exactly one entry = the last call** (8 + 7,802 + 137 = 7,947). The compaction
    trigger wants "how big a prefix the next round's resume must resend", which is
    the context the last call saw; the sum would count the prefix of a round with 20
    tool calls 20 times (the ledger measured 2.5 million to 100 million, always over
    the 300k threshold -> every working round followed by a compaction round).

    A wrong shape (no `iterations`, not a list, the last entry not a dict, or that
    entry without a single usable number) always returns None, letting the caller
    fall back to the old summed estimate -- that direction is "compact too early",
    which is bounded and never misses a compaction. Entries whose `type` is not
    `"message"` (none seen so far) are skipped, searching back for the last real
    model call. Never raises."""
    usage = result_ev.get("usage") if isinstance(result_ev, dict) else None
    iterations = usage.get("iterations") if isinstance(usage, dict) else None
    if not isinstance(iterations, list):
        return None
    for entry in reversed(iterations):
        if not isinstance(entry, dict):
            return None
        kind = entry.get("type")
        if kind is not None and kind != "message":
            continue
        if all(_dorossi_count(entry.get(k)) is None for k in _CONTEXT_KEYS):
            return None
        return _dorossi_usage_int(entry, _CONTEXT_KEYS)
    return None


_CLI_COMMAND_RE = re.compile(r"^/([A-Za-z][A-Za-z0-9_-]{0,31})(?:\s|$)")


def _dorossi_cli_command_of(prompt) -> str:
    """Is what this round sends a CLI slash command (`/compact` and the like) rather
    than words for the model? If so return the command name (lower-case, without
    `/`), otherwise an empty string. Never raises.

    Its use is to **mark maintenance rounds in the ledger** (the `k` field of
    `dorossi_usage.ndjson`), so that "this spend was compaction maintenance, not
    work" is visible without inferring it from timing correlations. Measured
    2026-08-30: in that period compaction was 9.9% of total spend ($39.73 /
    $401.15), and before the marker appeared those rows looked exactly like ordinary
    working rounds.

    **This is not there to silence the diagnostic.** The "spent money but no tokens
    readable" diagnostic still applies to maintenance rounds -- because measurement
    shows maintenance rounds' numbers **are** readable (see the note on
    `_dorossi_cc_round_info`), so not being able to read them really is a
    regression.
    """
    if not isinstance(prompt, str):
        return ""
    match = _CLI_COMMAND_RE.match(prompt.lstrip())
    return match.group(1).lower() if match else ""


def _dorossi_cc_round_info(result_ev: dict, *, stderr_tail: str = "",
                           cli_command: str = "") -> dict:
    """Per-invocation usage/cost summary parsed from a claude_code `result`
    event, returned as the 3rd element of `_dorossi_via_claude_code` so the
    autonomous loop can accumulate spend for budget-awareness / the periodic-
    compaction trigger. A plain dict keeps it extensible and never raises.
    Empty/missing event → 0.0 cost / 0 tokens.

    Keeps the `cost_usd` key UNCHANGED (B2's compaction trigger depends on it).
    The input token count is SPLIT (not folded) into three diagnostic buckets so
    the owner-only `@bot tokens` chart can tell cheap cache HITS from expensive
    cache REBUILDS (i.e. whether B1/B2's caching actually pays off):
      * `in`  = fresh input tokens
      * `cr`  = cache-read input tokens      (cheap — a cache hit)
      * `cc`  = cache-creation input tokens  (expensive — a cache rebuild)
      * `out` = output tokens
    Each read defensively (missing / non-numeric / bool → 0). These numbers are
    diagnostics / owner-only chart DATA — they NEVER reach Discord.

    **`modelUsage` is the preferred source; the top-level `usage` is only a
    fallback** (changed 2026-08-30): `usage` counts only the main model, while
    `total_cost_usd` is the total across all models, so putting the two in one row
    would not reconcile. Details in `_dorossi_model_usage_totals`.
    """
    cost = 0.0
    fresh = cread = ccreate = out = 0
    ctx = None
    if isinstance(result_ev, dict):
        raw = result_ev.get("total_cost_usd")
        if (isinstance(raw, (int, float)) and not isinstance(raw, bool)
                and math.isfinite(raw) and raw >= 0):
            # A non-finite amount (`json.loads` accepts NaN) would turn
            # `cost_since_compact` into nan, and every comparison with nan returns
            # False -- the spend trigger would then quietly stop working.
            cost = float(raw)
        ctx = _dorossi_last_call_context(result_ev)
        totals = _dorossi_model_usage_totals(result_ev)
        if totals is not None:
            fresh, cread = totals["in"], totals["cr"]
            ccreate, out = totals["cc"], totals["out"]
        else:
            usage = result_ev.get("usage")
            if isinstance(usage, dict):
                fresh = _dorossi_usage_int(usage, ("input_tokens",))
                cread = _dorossi_usage_int(usage, ("cache_read_input_tokens",))
                ccreate = _dorossi_usage_int(usage, ("cache_creation_input_tokens",))
                out = _dorossi_usage_int(usage, ("output_tokens",))
        if cost > 0.0 and not (fresh or cread or ccreate or out):
            # `stderr_tail` is needed only on this one anomaly path, so it is a
            # keyword argument defaulting to an empty string -- a normal round does
            # not need it, and normal rounds are the vast majority.
            # Spend with not a single token readable = this result event's shape is
            # not what we expect. Silently recording 0 would add a "spent money, used
            # no tokens" data point to `dorossi_usage.ndjson`, whose purpose is
            # exactly to reconcile tokens against money.
            #
            # **The cause of those 19 rows in the old ledger has been pinned down
            # (2026-08-30, reproducible across two real runs):** they are the
            # self-loop's `/compact` maintenance rounds. The result event of a
            # `/compact` round **does** have the `usage` key, but its five numbers
            # are **all 0**; the real numbers appear only in `modelUsage` (measured
            # in=2063, out=1661, cache_read=18115, with `costUSD` exactly equal to
            # `total_cost_usd`). That is, the earlier verdict recorded as "/compact
            # ruled out -- usage measured complete" only checked whether the key was
            # there, not its values.
            # And since this file today switched to **reading `modelUsage` first**,
            # this class is readable -- feeding the real event into
            # `_dorossi_cc_round_info` yields those four non-zero numbers. The old
            # data is still there because the live bot is a process started on
            # 2026-08-26 that has not loaded this code yet.
            # **So reaching here is a regression**: not even `modelUsage` is
            # readable, and the ledger will not reconcile.
            # Print only key names and subtype, not values (anything could be in
            # here), and only to stderr / the log, never to Discord.
            tail = (f" stderr tail: {stderr_tail[-300:]!r}"
                    if stderr_tail else "")
            print(f"[dorossi] result event has cost {cost:.4f} but no usage: "
                  f"subtype={result_ev.get('subtype')!r} "
                  f"is_error={result_ev.get('is_error')!r} "
                  f"keys={sorted(result_ev)}{tail}", file=sys.stderr)
    info = {"cost_usd": cost, "in": fresh, "cr": cread,
            "cc": ccreate, "out": out}
    if ctx is not None:
        # The last API call's context size (see `_dorossi_last_call_context`). Put in
        # only when readable; absent = "fall back to the summed estimate", handled
        # by `_dorossi_context_tokens`.
        info["ctx"] = ctx
    if cli_command:
        # Recorded in the ledger, so the "spend, 0 tokens" rows explain themselves
        # without any more inference.
        info["kind"] = cli_command
    return info


# ---------------------------------------------------------------------------
# Per-invocation money / tokens: in 2.1.277 the CLI made `--resume` totals
# cumulative per session
#
# 2.1.277 (2026-09-18) changelog: "Fixed a headless resume (`claude -p --resume`, …)
# starting the session's cost and usage totals at zero; headless sessions now save their
# totals at exit". Measured 2026-09-19 by calling an isolated 2.1.277 and the local
# 2.1.276 three times in a row each on the same session: 2.1.276's `total_cost_usd`
# went 0.0141 -> 0.0010 -> 0.0010 (per invocation); 2.1.277 went 0.0134 -> 0.0144 ->
# 0.0154 (**cumulative for the session**), with `modelUsage` tokens cumulative too;
# the top-level `usage` and `usage.iterations` are **per invocation** in both
# versions. The result event has no "this invocation" money field at all, yet the
# official SDK cost docs still say "each result reflects only that call" --
# upstream semantics are still moving, so this must be right for both, and cannot
# rely on the docs.
#
# Why this matters: the self-loop sums each round's `cost_usd` into
# `cost_since_compact` (the spend trigger defaults to $10), and the ledger records
# it round by round. Adding cumulative values round by round is O(N²): a session
# that has been running for a while would "exceed $10" every round -> a
# compaction round inserted after every round. And since the bot starts the CLI
# afresh each round, **it bites as soon as the CLI auto-updates, no bot restart
# needed**.
# ---------------------------------------------------------------------------
# From this version on, `--resume`'s `total_cost_usd` / `modelUsage` are cumulative
# per session.
_CLAUDE_CUMULATIVE_TOTALS_FROM = (2, 1, 277)
_CLI_VERSION_RE = re.compile(r"^\s*(\d{1,4})\.(\d{1,4})\.(\d{1,6})(?!\d)")
_ACCOUNT_TOKEN_KEYS = ("in", "cr", "cc", "out")


def _dorossi_cc_version_tuple(version) -> tuple | None:
    """`"2.1.277"` (the init event's `claude_code_version`) -> `(2, 1, 277)`; None
    when unreadable."""
    if not isinstance(version, str):
        return None
    match = _CLI_VERSION_RE.match(version)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _dorossi_cc_totals_mode(version) -> str | None:
    """Whether this CLI version's `--resume` reports totals as `"cumulative"` or
    `"per_call"`; None when the version is unreadable (left to
    `_dorossi_cc_account_round` to decide what to do)."""
    parsed = _dorossi_cc_version_tuple(version)
    if parsed is None:
        return None
    return "cumulative" if parsed >= _CLAUDE_CUMULATIVE_TOTALS_FROM else "per_call"


def _dorossi_usage_mark_of(mark) -> dict | None:
    """Validate the cumulative baseline stored in the session slot
    (`cc_usage_mark`) once before using it.

    This data comes from disk (`dorossi_session.json`, hand-editable locally), and
    a wrong shape is always treated as "no baseline" -- in cumulative mode that
    direction means "this round's money is not counted", which never inflates.
    Never raises."""
    if not isinstance(mark, dict):
        return None
    sid = mark.get("sid")
    mode = mark.get("mode")
    cost = mark.get("cost")
    if not isinstance(sid, str) or not sid:
        return None
    if mode not in ("cumulative", "per_call", None):
        return None
    if (isinstance(cost, bool) or not isinstance(cost, (int, float))
            or not math.isfinite(cost) or cost < 0):
        return None
    out = {"sid": sid, "mode": mode, "cost": float(cost),
           "tok": mark.get("tok") if mark.get("tok") in ("model", "usage") else None}
    for key in _ACCOUNT_TOKEN_KEYS:
        value = _dorossi_count(mark.get(key))
        if value is None:
            return None
        out[key] = value
    return out


def _dorossi_per_call(current: list, cumulative: bool, base, floor: list):
    """A counter's "this invocation" value: returns (value, label).

    * Not cumulative -> the raw value (`"call"`).
    * Cumulative but no baseline -> `floor` (`"base"`): there is no telling how much
      this invocation accounts for, so **undercount rather than inflate** --
      counting the whole session's total as one round is exactly the defect being
      fixed.
    * A negative delta, or a delta sum clearly smaller than the sum of `floor`
      (`floor` is the amount this invocation **certainly** has at least) -> the
      counter restarted (compaction, session rebuild …) or the baseline does not
      belong to this stretch; return the raw value (`"reset"`). "Clearly" leaves a
      little tolerance (5% + 64): the two sets of numbers come from two different
      sums in the CLI, and a few tokens of difference should not knock a correct
      delta down to reset -- reset returns the raw value, which in cumulative mode
      is the inflating direction.
    * Otherwise -> the delta (`"delta"`).
    """
    if not cumulative:
        return list(current), "call"
    if base is None:
        return list(floor), "base"
    delta = [c - b for c, b in zip(current, base)]
    floor_total = sum(floor)
    tolerance = 64 + floor_total * 0.05
    if any(d < 0 for d in delta) or sum(delta) + tolerance < floor_total:
        return list(current), "reset"
    return delta, "delta"


def _dorossi_cc_account_round(info: dict, result_ev, *, resumed_id=None, sid=None,
                              cli_version=None, baseline=None) -> dict:
    """Convert the raw numbers of `_dorossi_cc_round_info` into **this
    invocation's** numbers, and attach the cumulative baseline for next time
    (`usage_mark`). Pure function, never raises; returns a new dict and does not
    modify `info`.

    Rules (must be right for both CLI semantics):
    * No `--resume` (a new session) -> the reported value is this invocation, the
      same in both versions.
    * Version < 2.1.277 (`per_call`) -> the reported value is this invocation.
    * Version ≥ 2.1.277 (`cumulative`) -> this invocation = the current cumulative
      total − the cumulative total last saved for the same session (`baseline`).
      The baseline counts only when "`sid` equals this resume's id, and it was also
      cumulative mode at the time": 2.1.276 saved **per-invocation** values, and
      using one as a cumulative baseline would count the whole session as one round
      (every session would hit this on upgrade day).
    * Version unreadable -> keep the same session's previous mode
      (`baseline["mode"]`); without even that, treat it as `per_call`, i.e. the
      behaviour before this change.
    * Cumulative mode with no usable baseline (the first resume after upgrading, or
      the slot's baseline lost) -> money recorded as 0, tokens as the top-level
      `usage` (**per invocation and main model only in both versions**, the amount
      this invocation certainly has at least). This is a deliberate trade-off: when
      it cannot be told apart, undercount one round rather than count the whole
      session as one round; the saved baseline makes it exact from the next round
      on. The label `"base"` goes into the ledger and can be looked up afterwards.
    * A negative delta (the counter restarted) or one smaller than the top-level
      `usage` (the baseline does not belong to this stretch) -> treated as starting
      from zero, using the reported value (label `"reset"`).

    Tokens and money are two counters: tokens accumulate only when they come from
    `modelUsage` (the cumulative one); the top-level `usage` fallen back on when
    `modelUsage` is absent is per invocation anyway. The two share one baseline, so
    if either is judged reset, the other resets with it.

    `usage_mark` always records the **raw reported values** (not the converted
    ones), because the next round subtracts it from the raw reported values. It is
    present only when the backend's session id is known.
    """
    out = dict(info) if isinstance(info, dict) else {}
    raw_cost = out.get("cost_usd")
    if (isinstance(raw_cost, bool) or not isinstance(raw_cost, (int, float))
            or not math.isfinite(raw_cost) or raw_cost < 0):
        raw_cost = 0.0
    raw_cost = float(raw_cost)
    raw_tokens = [_dorossi_count(out.get(k)) or 0 for k in _ACCOUNT_TOKEN_KEYS]
    usage = result_ev.get("usage") if isinstance(result_ev, dict) else None
    floor_tokens = [_dorossi_usage_int(usage, (key,)) for key in (
        "input_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "output_tokens")]
    token_source = ("model" if _dorossi_model_usage_totals(result_ev) is not None
                    else "usage")

    base = _dorossi_usage_mark_of(baseline)
    if base is not None and (not resumed_id or base["sid"] != resumed_id):
        base = None
    mode = _dorossi_cc_totals_mode(cli_version)
    if mode is None and base is not None:
        mode = base["mode"]
    cumulative = bool(resumed_id) and mode == "cumulative"
    cum_base = base if base is not None and base["mode"] == "cumulative" else None

    cost_vals, cost_label = _dorossi_per_call(
        [raw_cost], cumulative,
        None if cum_base is None else [cum_base["cost"]], [0.0])
    token_cumulative = cumulative and token_source == "model"
    token_base = (None if cum_base is None or cum_base["tok"] != "model"
                  else [cum_base[k] for k in _ACCOUNT_TOKEN_KEYS])
    token_vals, token_label = _dorossi_per_call(
        raw_tokens, token_cumulative, token_base, floor_tokens)
    # The two counters share one baseline: if either judges "the baseline does not
    # belong to this stretch", the other's delta cannot be trusted either (how could
    # the same baseline be right for money and wrong for tokens), so both start
    # from the raw value together.
    if "reset" in (cost_label, token_label):
        if cost_label == "delta":
            cost_vals, cost_label = [raw_cost], "reset"
        if token_label == "delta":
            token_vals, token_label = list(raw_tokens), "reset"

    out["cost_usd"] = round(max(0.0, cost_vals[0]), 10)
    for key, value in zip(_ACCOUNT_TOKEN_KEYS, token_vals):
        out[key] = int(value)
    out["acct"] = (cost_label if cost_label == token_label
                   else f"{cost_label}/{token_label}")
    if isinstance(cli_version, str) and cli_version:
        out["v"] = cli_version[:32]
    if isinstance(sid, str) and sid:
        mark = {"sid": sid, "mode": mode, "cost": raw_cost, "tok": token_source}
        mark.update(zip(_ACCOUNT_TOKEN_KEYS, raw_tokens))
        out["usage_mark"] = mark
    return out


def _dorossi_trim_usage_file() -> None:
    """Keep DOROSSI_USAGE_FILE bounded: once it grows past _DOROSSI_USAGE_TRIM_AT
    lines, atomically rewrite it to the last _DOROSSI_USAGE_MAX_LINES (hysteresis
    avoids rewriting on every append once near the cap). Never raises."""
    tmp = DOROSSI_USAGE_FILE.with_name(DOROSSI_USAGE_FILE.name + ".tmp")
    try:
        if not DOROSSI_USAGE_FILE.exists():
            return
        with open(DOROSSI_USAGE_FILE, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) <= _DOROSSI_USAGE_TRIM_AT:
            return
        keep = lines[-_DOROSSI_USAGE_MAX_LINES:]
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(keep)
        os.replace(tmp, DOROSSI_USAGE_FILE)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] usage trim failed: {exc!r}", file=sys.stderr)
        # `os.replace` moves the temp away **only when it succeeds**. This function's
        # contract is never to raise outward, so it does not re-raise, but it must
        # not leave half the data in the repo root either: left there, the next trim
        # would simply overwrite it (harmless), but it would keep lying around
        # looking like real data, and it is exactly the kind of "repo-root runtime
        # artefact" `test_gitignore_coverage.py` watches for.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _dorossi_record_usage(info: dict) -> None:
    """Append ONE token-usage data point (built from a round-info dict) to
    DOROSSI_USAGE_FILE, then trim. Writes the SPLIT schema
    `{"ts","in","cr","cc","out","cost_usd"}` (in=fresh / cr=cache_read /
    cc=cache_creation / out=output). Fail-soft: any error is logged to stderr
    and swallowed — recording usage must NEVER break a Dorossi turn nor surface
    to Discord. Missing fields are recorded as 0."""
    info = info if isinstance(info, dict) else {}

    def _i(key):
        v = info.get(key)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    try:
        rec = {
            "ts": time.time(),
            "in": _i("in"),
            "cr": _i("cr"),
            "cc": _i("cc"),
            "out": _i("out"),
            "cost_usd": (float(info.get("cost_usd") or 0.0)
                         if isinstance(info.get("cost_usd"), (int, float))
                         and not isinstance(info.get("cost_usd"), bool) else 0.0),
        }
        kind = info.get("kind")
        if isinstance(kind, str) and kind:
            # The marker for a maintenance round (a CLI slash command). Written only
            # when non-empty, so ordinary working rows stay as they were and old data
            # and old readers are unaffected. The length is already capped at 32
            # characters by `_CLI_COMMAND_RE`.
            rec["k"] = kind[:32]
        # The next three fields were all added 2026-09-19 and are written only when
        # they have a value, so old rows and old readers are unaffected:
        # * `ctx`  the last API call's context size (exactly what the compaction
        #          trigger uses). With it, "why this round did (not) compact" can be
        #          reconciled afterwards; in/cr/cc are sums over the whole
        #          invocation and cannot be.
        # * `acct` how money / tokens were converted into "this invocation" (see
        #          `_dorossi_cc_account_round`). `"call"` is not written (it is the
        #          reported value itself, the same meaning as old rows).
        # * `v`    the CLI version. The ledger spans the 2.1.277 semantic change, and
        #          without this field there is no telling which rows were converted.
        ctx = _dorossi_count(info.get("ctx"))
        if ctx is not None:
            rec["ctx"] = ctx
        acct = info.get("acct")
        if isinstance(acct, str) and acct and acct != "call":
            rec["acct"] = acct[:16]
        version = info.get("v")
        if isinstance(version, str) and version:
            rec["v"] = version[:32]
        with open(DOROSSI_USAGE_FILE, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        _dorossi_trim_usage_file()
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] usage record failed: {exc!r}", file=sys.stderr)


def _dorossi_round_info_and_record(result_ev: dict, *,
                                   stderr_tail: str = "",
                                   cli_command: str = "",
                                   resumed_id: str | None = None,
                                   sid: str | None = None,
                                   cli_version: str | None = None,
                                   baseline: dict | None = None) -> dict:
    """Build the per-invocation round-info dict AND append it to the usage log.
    Used at every `_dorossi_via_claude_code` return so single-turn and every
    autonomous-loop round each contribute one data point.

    What is returned is **this invocation's** numbers (converted by
    `_dorossi_cc_account_round`), not the CLI's raw reported values: from 2.1.277 on,
    `--resume` reports session-cumulative totals, and summing them directly
    (`cost_since_compact`) or ledgering them would double count.
    `resumed_id` / `sid` / `cli_version` / `baseline` are the four things the
    conversion needs (this resume's id, the id the backend reported, the init
    event's version, the previous cumulative total stored in the slot); all omitted
    = a new session, with the same behaviour as before the change. The return value
    carries an extra `usage_mark`, which the caller must save back into the same
    slot so the next round has a baseline.

    `stderr_tail` is merely passed down to the "spent money but no tokens readable"
    diagnostic; every failure path already prints the stderr tail, and only the
    success path dropped it. That diagnostic looks at the **raw** reported values.

    `cli_command` marks "this round sent a CLI slash command rather than words for
    the model" and is written into the ledger's `k` field, so maintenance rounds'
    spend can be told apart from working rounds' (measured: compaction was 9.9% in
    that period). It does **not affect** the diagnostic above -- maintenance rounds'
    numbers are readable, and not being able to read them is a regression.
    """
    info = _dorossi_cc_round_info(result_ev, stderr_tail=stderr_tail,
                                  cli_command=cli_command)
    info = _dorossi_cc_account_round(info, result_ev, resumed_id=resumed_id, sid=sid,
                                     cli_version=cli_version, baseline=baseline)
    _dorossi_record_usage(info)
    return info


def _dorossi_read_usage(limit: int) -> list[dict]:
    """Return up to the last `limit` usage data points (oldest → newest) parsed
    from DOROSSI_USAGE_FILE. Never raises: a missing / unreadable file yields [],
    and malformed lines are skipped. Backend DATA for the owner-only chart."""
    try:
        if not DOROSSI_USAGE_FILE.exists():
            return []
        with open(DOROSSI_USAGE_FILE, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] usage read failed: {exc!r}", file=sys.stderr)
        return []
    records: list[dict] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = _json.loads(raw)
        except Exception:  # pylint: disable=broad-except
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records[-limit:] if limit > 0 else records


def dorossi_local_usage_totals(now: float, path=DOROSSI_USAGE_FILE) -> dict:
    """Usage subtotals from the local Dorossi ledger (`DOROSSI_USAGE_FILE`), used by
    the owner-only `/dorossi tokens` to build its first section. Returns three
    cells, each `{"tokens": int, "usd": float}`:

    * `today`    -- the same **local calendar day** as `now` (from 00:00 of that day
      in the host's time zone).
    * `last7d`   -- a rolling last 7 days (`ts >= now - 7*86400`).
    * `lifetime` -- the total over every valid row in the ledger.

    `now` is "now" in epoch seconds (injected from outside for testing). `tokens`
    counts only `in` + `out` (consistent with the old chart's definition of `in` /
    `out`, excluding cache); `usd` simply sums each row's `cost_usd` (= the
    `total_cost_usd` the backend CLI reports; no home-grown price table).

    **Never raises, streams line by line**: a missing / unreadable file, and any
    row that is bad JSON / not a dict / has `ts` missing or non-finite, is skipped
    (counted into no cell), and reading line by line never loads the whole ledger
    into memory (append-only NDJSON can get large)."""
    def _blank():
        return {"tokens": 0, "usd": 0.0}

    def _num(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    today, last7d, lifetime = _blank(), _blank(), _blank()
    try:
        lt = time.localtime(now)
        day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                 0, 0, 0, 0, 0, -1))
    except (ValueError, OverflowError, OSError):
        day_start = now
    week_start = now - 7 * 86400

    try:
        p = path if isinstance(path, Path) else Path(path)
        if not p.exists():
            return {"today": today, "last7d": last7d, "lifetime": lifetime}
        with open(p, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = _json.loads(raw)
                except Exception:  # pylint: disable=broad-except
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = _num(rec.get("ts"))
                if ts is None:
                    continue
                tok = (int(_num(rec.get("in")) or 0)
                       + int(_num(rec.get("out")) or 0))
                cost = float(_num(rec.get("cost_usd")) or 0.0)
                lifetime["tokens"] += tok
                lifetime["usd"] += cost
                if ts >= week_start:
                    last7d["tokens"] += tok
                    last7d["usd"] += cost
                if ts >= day_start:
                    today["tokens"] += tok
                    today["usd"] += cost
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] local usage totals failed: {exc!r}", file=sys.stderr)
    return {"today": today, "last7d": last7d, "lifetime": lifetime}


def _dorossi_context_tokens(info: dict) -> int:
    """The context size (tokens) at the end of this round, for the two compaction
    triggers. Never raises, returns an int ≥ 0.

    Prefers `info["ctx"]` = the **last API call's** in + cache_read + cache_creation
    (see `_dorossi_last_call_context`): that is the prefix the next round's resume
    will resend.

    **Before 2026-09-19 this was `in + cr + cc`, and those three numbers are sums
    over every API call in the whole invocation** (with `modelUsage` adding the
    other models on top). A full-mode round easily makes dozens of tool calls, so
    the same prefix was counted dozens of times: the ledger measured 2.5 million to
    100 million, always over the 300k threshold, and **every working round was
    followed by a compaction round** (the last 40 rows strictly alternating, each
    $0.3–4.5, about 3 minutes, and losing detail every round). The old docstring's
    "overestimating only compacts a bit early" did not hold -- the overestimate's
    multiple equals the number of tool calls.

    Only when `ctx` is absent (`iterations` unreadable) does it fall back to that
    sum -- the direction is compacting too early, which is bounded and never misses
    a compaction."""
    if isinstance(info, dict):
        ctx = _dorossi_count(info.get("ctx"))
        if ctx is not None:
            return ctx
    return _dorossi_usage_int(info, ("in", "cr", "cc"))


def _dorossi_context_compaction_due(context_tokens: int) -> bool:
    """True when a round's context size (see `_dorossi_context_tokens`) has
    reached the configured token threshold `DOROSSI_COMPACT_CONTEXT_TOKENS`
    (0 = disabled). Shared by BOTH the single-turn chat path and the autonomous
    loop, so both compact on the same context-size boundary. The owner-mandated
    token-reduction lever — no effort/model/tool changes."""
    return (DOROSSI_COMPACT_CONTEXT_TOKENS > 0
            and context_tokens >= DOROSSI_COMPACT_CONTEXT_TOKENS)


def _dorossi_loop_compaction_due(rounds_since_compact: int,
                                 cost_since_compact: float,
                                 context_tokens: int = 0) -> bool:
    """True when the autonomous loop should insert a periodic-compaction
    maintenance round: `rounds_since_compact` has reached the configured round
    interval, OR `cost_since_compact` has crossed the configured dollar threshold,
    OR the last round's `context_tokens` has crossed the configured token
    threshold (`_dorossi_context_compaction_due`, the same key the single-turn
    path uses). Each counter is reset right after a compaction; each threshold is
    independently disabled by setting its config key to 0. `context_tokens`
    defaults to 0 (kept keyword-defaulted so pre-existing callers/tests keep
    working) which, together with the >0 guard, is a no-op for the token
    condition. Bounds the O(N²) growth of the unbounded resumed conversation
    WITHOUT capping rounds."""
    if (DOROSSI_LOOP_COMPACT_EVERY_ROUNDS > 0
            and rounds_since_compact >= DOROSSI_LOOP_COMPACT_EVERY_ROUNDS):
        return True
    if (DOROSSI_LOOP_COMPACT_COST_USD > 0
            and cost_since_compact >= DOROSSI_LOOP_COMPACT_COST_USD):
        return True
    if _dorossi_context_compaction_due(context_tokens):
        return True
    return False


def _dorossi_api_is_usage_limit(exc: Exception, rate_err) -> bool:
    """True when an Anthropic SDK error represents a plan/quota usage limit:
    a 429 RateLimitError, a 402 billing error, or a status/message that names
    a rate/usage limit. Tolerant of the SDK being absent (rate_err == ()).
    Never raises — undecidable reads as False."""
    # The whole thing is wrapped for the same reason as
    # `_dorossi_api_transient_error` (`exc` comes from a third-party SDK,
    # `status_code` may be a property that blows up, and `__str__` may blow up too),
    # but here it is **even more necessary**: this function runs before that one,
    # and if it blows up first, that one's guard never runs at all, the whole
    # `except` block is replaced by a baffling new exception, and the original
    # error vanishes along with its traceback.
    #
    # The default of `getattr(exc, "status_code", None)` only swallows
    # AttributeError; any other exception a property raises still propagates, so
    # that line on its own is no guard.
    #
    # Returning False when undecidable is the safe direction: it hands over to the
    # transient verdict, and failing that a bare `raise`, i.e. exactly the behaviour
    # from before this layer was added. Returning True would instead make the
    # self-loop sleep for hours over a limit that could not be confirmed.
    try:
        if rate_err and isinstance(exc, rate_err):
            return True
        status = getattr(exc, "status_code", None)
        if status in (429, 402):
            return True
        msg = str(exc).lower()
        return ("rate_limit" in msg or "rate limit" in msg
                or "usage limit" in msg or "billing" in msg)
    except Exception:  # pylint: disable=broad-except
        return False


def _dorossi_api_retry_after_sec(exc: Exception) -> float | None:
    """Read the `retry-after` header (seconds) off an Anthropic SDK error.
    Returns None when the header is missing / unusable. Never raises.

    Deliberately split from `_dorossi_api_reset_hint`: this one's return value **is
    used to compute a wait in seconds** (the self-loop sleeps until the quota
    returns), while that one is just a string for people. Merged into one, it would
    have to pick between "seconds" and "a formatted string", and both sides would
    make do."""
    try:
        resp = getattr(exc, "response", None)
        headers = getattr(resp, "headers", None)
        if not headers:
            return None
        retry_after = headers.get("retry-after")
        if not retry_after:
            return None
        secs = float(retry_after)
        # NaN / inf cannot be stopped by `<= 0` (`nan <= 0` is False, and `inf`
        # passes straight through), and float("nan") / float("inf") are both legal
        # float() input -- the header is an external string, always treated as
        # untrusted.
        if not (secs > 0) or secs != secs or secs == float("inf"):
            return None
        return secs
    except Exception:  # pylint: disable=broad-except
        return None


def _dorossi_api_reset_hint(exc: Exception) -> str | None:
    """Read the `retry-after` header off an Anthropic SDK error and render it as
    a wall-clock reset time. Returns None when the header is missing / unusable.
    Never raises."""
    secs = _dorossi_api_retry_after_sec(exc)
    if secs is None:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(time.time() + int(secs)))
    except Exception:  # pylint: disable=broad-except
        return None


def _dorossi_api_transient_error(exc: Exception
                                ) -> "_DorossiTransientError | None":
    """Whether an SDK exception looks like "a transient server failure"; if so
    return a filled-in exception, otherwise None.

    Same shape as `_dorossi_cc_transient_error` / `_dorossi_codex_transient`, **and
    it must come after the usage-limit verdict**: 429 also means "come back
    later", but it has its own waiting strategy (read `retry-after` / wait for the
    quota to reset), far more precise than the exponential backoff here.

    Added 2026-09-05. Before this the api path had **no such verdict at all**:
    everything other than 429 / 402 (including 529 Overloaded and 5xx) was a bare
    `raise` that fell into the self-loop's generic `except`, handled by a short
    backoff of 20s→40s→80s at most three times -- under two and a half minutes in
    total, while the 2026-09-03 overload lasted well over several minutes. The
    other two backends already took their own long backoff (from 30s, capped at 15
    minutes, at most 20 rounds); this just brings the api path in line.

    The verdict looks at both `status_code` (carried by the SDK's `APIStatusError`
    family) and the message wording: a connection-layer failure
    (`APIConnectionError`) has no status code and leaves only text.
    """
    # The whole thing is wrapped because neither input is under our control: `exc`
    # comes from a third-party SDK, `status_code` may be a property (reading it can
    # blow up), and `__str__` may blow up too. This is called inside an `except`
    # block -- raising here would replace a "just wait a bit" server error with a
    # baffling new exception, and the original error would vanish along with its
    # traceback. Returning None when undecidable (= not transient) is the safe
    # direction: the caller falls back to a bare `raise`, i.e. the behaviour from
    # before this verdict was added.
    try:
        status = getattr(exc, "status_code", None)
        try:
            status_int = int(status) if status not in (None, "") else None
        except (TypeError, ValueError):
            status_int = None
        low = f"{type(exc).__name__}: {exc}".lower()
        hit = (status_int in DOROSSI_TRANSIENT_STATUSES
               or any(marker in low for marker in _TRANSIENT_TEXT_MARKERS))
        if not hit:
            return None
        return _DorossiTransientError(
            (str(exc).strip() or f"status_code={status_int}")[:400],
            status=status_int)
    except Exception:  # pylint: disable=broad-except
        return None


def _dorossi_trim_api_history(history, cap: int | None = None) -> list:
    """Trim the history the `api` backend resends down to the last `cap` messages,
    aligned to start at a user message.

    Only the `api` backend takes this path. The API is stateless, so the whole
    history is sent again every round; untrimmed, input tokens grow linearly with
    the round count (total cost is quadratic in rounds), and sooner or later it
    exceeds the context window and gets a 400. **A 400 is not a transient error**,
    so the self-loop would retry with the same over-long history until it gave up,
    and every round would then fail in exactly the same way -- with no path to
    self-repair unless someone knew to issue `/new`. The sliding window drops the
    earliest context, but "remembering a bit less" is far better than "broken from
    now on".

    Aligning to user is necessary, not tidiness: the Messages API does not accept
    `messages` starting with assistant, and cutting in the middle lands right on an
    assistant message half the time. It drops one more message going forward
    rather than keeping one more going back -- keeping one more would exceed the
    cap, meaning the bound would sometimes not hold.

    **Aligning is unconditional; only trimming is conditional.** A history that is
    within the cap but itself starts with assistant (those saved before the bound
    went live are exactly that) is still bounced by the API with a 400, so aligning
    cannot happen only when trimming. `cap <= 0` = no limit (the same convention as
    `dorossi_max_budget_usd`), but even without a limit it still aligns.

    When it actually changes something it writes one stderr line: this is expected
    behaviour, not a fault, but "why did the answer forget what was said earlier"
    will be investigated some day, and with no record at that point there is
    nothing to find. When nothing changes it stays quiet -- a no-news line printed
    every round ends up read by nobody.
    """
    if cap is None:
        cap = DOROSSI_API_HISTORY_MAX_MSGS
    rows = [r for r in (history or []) if isinstance(r, dict)]
    kept = rows if (cap <= 0 or len(rows) <= cap) else rows[-cap:]
    if kept and kept[0].get("role") != "user":
        kept = list(kept)
        while kept and kept[0].get("role") != "user":
            kept.pop(0)
    if len(kept) != len(rows):
        print(f"dorossi api history trimmed: {len(rows)} -> {len(kept)} "
              f"messages (cap {cap})", file=sys.stderr)
    return kept


async def _dorossi_via_api(prompt: str, history: list,
                           model: str | None = None) -> tuple[str, list]:
    """Answer via the Anthropic API (SDK), carrying `history` for continuity.
    Returns (answer, new_history). The API is stateless, so the history is
    resent each turn — bounded to the last `dorossi_api_history_max_msgs`
    messages by `_dorossi_trim_api_history`, because "resend everything" with no
    ceiling ends in a 400 that never recovers. Raises on missing client / API
    error (the SDK defers the credential check to request time).

    `model` is the **full model id** parsed from this round's `/model` (the output
    of `dorossi_resolve_model`, which has a value only when the allowlist lookup
    hits); None = this session did not specify one, use `DOROSSI_MODEL`. Before
    2026-09-23 this was hard-coded to `DOROSSI_MODEL`, so `/model` **silently did
    nothing** on this backend: the command replied "updated", yet the same model
    was always sent. This path has no CLI alias resolution, so a bare alias
    (`opus`) must be turned into a concrete id upstream -- that is the third path
    of `dorossi_resolve_model` (model catalogue -> newest of the same family in the
    built-in table)."""
    cli = _get_dorossi_client()
    if cli is None:
        raise RuntimeError("anthropic SDK client unavailable")
    # Trimming happens **before sending**, so the bound governs both this round's
    # cost and the copy saved back (`new_history` grows from `msgs` and is at most
    # two messages over the cap, reclaimed the next round).
    msgs = _dorossi_trim_api_history(history) + [
        {"role": "user", "content": prompt}]
    # Map the SDK's 429 (and 402 billing) to the dedicated usage-limit error so
    # the caller can give an actionable plan/quota reply. RateLimitError exposes
    # the `retry-after` header (seconds) as the reset hint when present.
    rate_err = getattr(anthropic, "RateLimitError", ()) if anthropic else ()
    # This project's own outer frame (see the note above `DOROSSI_API_TIMEOUT_SEC`).
    # `asyncio.timeout` rather than `wait_for` is used for `expired()`: it can tell
    # "the outer frame ran out" from "some TimeoutError raised inside the SDK", and
    # the latter must still go through the classification below.
    ceiling = _dorossi_api_call_ceiling_sec()
    bound = asyncio.timeout(ceiling)
    try:
        async with bound:
            resp = await cli.messages.create(
                model=(model or DOROSSI_MODEL),
                max_tokens=DOROSSI_MAX_TOKENS,
                system=DOROSSI_SYSTEM_PROMPT,
                messages=msgs,
            )
    except Exception as exc:  # pylint: disable=broad-except
        if isinstance(exc, TimeoutError) and bound.expired():
            # Take the existing generic failure path: the message deliberately
            # contains no words like "unavailable" or "api_key" -- the api branch of
            # `_dorossi_error_hint` would read those as "no credentials".
            print(f"[dorossi] api call exceeded this project's {ceiling:.0f}s "
                  f"ceiling (request timeout x attempts + retry sleeps); giving up",
                  file=sys.stderr)
            raise TimeoutError(
                f"api call exceeded the {ceiling:.0f}s ceiling") from exc
        if _dorossi_api_is_usage_limit(exc, rate_err):
            retry_after = _dorossi_api_retry_after_sec(exc)
            raise _DorossiUsageLimitError(
                str(exc)[:300], _dorossi_api_reset_hint(exc),
                reset_at=(time.time() + retry_after
                          if retry_after is not None else None)) from exc
        # **The order is the rule**: the usage limit is judged first (its wait is
        # more precise), and only what remains is asked "is this a transient server
        # failure". The other way round, a 429 would be treated as an overload, and
        # a blindly guessed exponential backoff would replace the precise wait that
        # `retry-after` brings.
        transient_exc = _dorossi_api_transient_error(exc)
        if transient_exc is not None:
            raise transient_exc from exc
        # A connection-layer failure (no status code): wait for the network to come
        # back, then resend the same history. It comes after the usage limit and
        # transient failures, for the same reason as the Claude side.
        if _dorossi_api_is_offline(exc):
            raise _DorossiOfflineError(
                f"{type(exc).__name__}"[:400], backend="api") from exc
        raise
    answer = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()
    # Only advance history on a real answer (don't persist a dangling user turn).
    new_history = msgs + [{"role": "assistant", "content": answer}] if answer else history
    return answer, new_history


# ---- Host sleep: the watchdogs must not count time spent asleep (2026-09-22) ----
#
# This machine uses Modern Standby. While asleep the whole process is frozen, and
# on waking `time.monotonic()` has jumped a long way, so every watchdog (idle,
# output silence, hard limit) expires at once -- a round halfway through its work
# is cut as "idle too long" the instant it wakes, when it had merely been asleep
# along with the host.
#
# Approach: the watchdogs no longer `wait_for(readline, whole span)` in one go, but
# wait in slices of `_DOROSSI_WATCH_SLICE_SEC`; when a slice's actually elapsed time
# exceeds what it should have waited by `DOROSSI_SUSPEND_GAP_SEC` or more, the host
# is taken to have slept, that slice counts only the length it was meant to wait,
# and the excess is recorded in `_DorossiWatchClock.suspended` (the hard limit
# subtracts it too). The larger of two clocks is taken: whether `time.monotonic()`
# advances during sleep on this platform is uncertain, while the wall clock always
# does -- watching both measures it on any platform. A wall clock pushed forward by
# time sync is also read as "slept", at the cost only of that round's watchdog
# getting that many seconds of slack.
DOROSSI_SUSPEND_GAP_SEC = 30.0
_DOROSSI_WATCH_SLICE_SEC = 5.0
# Total seconds and count of sleep this process has detected so far (measured by
# any watchdog). For diagnostics only.
DOROSSI_SUSPEND_SEEN = {"count": 0, "seconds": 0.0}


def dorossi_note_suspend(gap: float) -> None:
    """Record one detected host sleep (seconds). Never raises."""
    try:
        DOROSSI_SUSPEND_SEEN["count"] += 1
        DOROSSI_SUSPEND_SEEN["seconds"] += float(gap)
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass


def dorossi_elapsed_with_gap(mono_start: float, wall_start: float,
                             expected: float) -> tuple:
    """For a wait that "should take `expected` seconds", how long actually passed and
    how much of that was the host asleep.

    Returns `(elapsed, suspended)`: `suspended` > 0 means this stretch overran the
    expectation by `DOROSSI_SUSPEND_GAP_SEC` or more (treated as sleep), in which
    case `elapsed` counts only `expected`."""
    elapsed = max(time.monotonic() - mono_start, time.time() - wall_start)
    gap = elapsed - expected
    if gap > DOROSSI_SUSPEND_GAP_SEC:
        return float(expected), float(gap)
    return float(elapsed), 0.0


class _DorossiWatchClock:
    """The watchdogs' clock: `time.monotonic()` minus the sleep measured this
    round."""

    __slots__ = ("suspended",)

    def __init__(self) -> None:
        self.suspended = 0.0

    def now(self) -> float:
        return time.monotonic() - self.suspended


async def _dorossi_readline_watched(stream, timeout: float,
                                    clock: "_DorossiWatchClock") -> bytes:
    """A sleep-aware version of `asyncio.wait_for(stream.readline(), timeout)`.

    The same `readline` keeps waiting across several slices (not restarted, so no
    data is lost); only when "time awake" adds up to `timeout` does it raise
    `asyncio.TimeoutError`. The seconds of sleep are recorded in `clock.suspended`
    and `DOROSSI_SUSPEND_SEEN`. On timeout or cancellation the unfinished
    `readline` is reaped -- a cancelled `StreamReader.readline` does not eat the
    data in the buffer, and the next call still reads it."""
    task = asyncio.ensure_future(stream.readline())
    try:
        waited = 0.0
        while True:
            step = max(0.0, min(_DOROSSI_WATCH_SLICE_SEC, timeout - waited))
            mono0, wall0 = time.monotonic(), time.time()
            done, _pending = await asyncio.wait({task}, timeout=step)
            if done:
                return task.result()
            elapsed, slept = dorossi_elapsed_with_gap(mono0, wall0, step)
            if slept:
                clock.suspended += slept
                dorossi_note_suspend(slept)
                print(f"[dorossi] host looks suspended for ~{slept:.0f}s; not "
                      "counting it against this round's watchdogs", file=sys.stderr)
            waited += elapsed
            if waited >= timeout:
                raise asyncio.TimeoutError()
    finally:
        if not task.done():
            task.cancel()


async def _read_stream_all(stream) -> bytes:
    """Drain an asyncio stream to EOF, swallowing errors (used for stderr so a
    full pipe can't deadlock the child while we read stdout).

    **It returns only at EOF, and EOF is not in our hands** -- it waits until every
    write-end handle of the pipe is closed. So this coroutine has no bound of its
    own, must always be reaped through `_dorossi_drain_stderr`, and must never be
    `await`ed directly anywhere.
    """
    try:
        return await stream.read()
    except Exception:  # pylint: disable=broad-except
        return b""


# The bound for clean-up. **This is not the bound for "waiting for the backend to
# finish its work"** (that is the watchdogs' job, default 900/10800 seconds); it is
# the bound for "the process should already have ended, reap it cleanly", so it is
# short. On the normal path this time is never even used: after stdout EOF the CLI
# exits within milliseconds and the pipes close with it.
_DOROSSI_REAP_TIMEOUT_SEC = 10.0
# The interval for glancing back at `proc.returncode`. This is not a polling wait --
# `wait()` returns the moment it completes (note that `_dorossi_reap_proc` uses
# `asyncio.wait`, not `sleep`); this value only decides how soon the rc can be
# noticed on the "pipes held open" path.
_DOROSSI_REAP_POLL_SEC = 0.05
# The sentinel handed to the verdict when not even the rc can be obtained. Non-zero
# -> it takes the existing "non-zero exit" classification, with no need for an
# extra branch in `_claude_stream_verdict` (its verdict order is itself the rule;
# do not touch it).
_DOROSSI_UNREAPED_RC = -9


async def _dorossi_reap_proc(proc, timeout: float | None = None) -> int | None:
    """Reap a child process that **should already have ended**, and report its rc
    within a bounded time (None when it cannot be obtained).

    **Why `await proc.wait()` alone will not do** (measured locally 2026-09-09,
    CPython 3.14 / Windows): `BaseSubprocessTransport._wait()` hangs itself on
    `_exit_waiters`, and those waiters are woken **only** by
    `_call_connection_lost`, while `_try_finish` requires `all(p.disconnected)` --
    that is, **both stdout and stderr must reach EOF first**. As long as one
    grandchild process inherits the write end of either pipe,
    `await proc.wait()` **never returns**; not slow, infinite. And grandchildren
    really do linger: Windows' `proc.kill()` is `TerminateProcess`, which takes
    only the direct child, and `dorossi_cc_tools="full"` is exactly the mode that
    starts shells on the host.

    `proc.returncode` takes a different route: `_process_exited` sets it the moment
    the OS-level exit is observed, **independent of the pipes**. Measured: 0.25
    seconds after kill the returncode is already 1, while a `wait()` pending at the
    same time is still unfinished three seconds later. (The order matters too --
    `_wait()` starts with `if self._returncode is not None: return`, so calling
    `wait()` **after it is set** returns immediately; only a wait that "started
    before it was set" gets stuck.)

    So this watches **both** signals at once, whichever comes first: `wait()`
    completing (the path where the pipes close normally, zero extra delay) and
    `returncode` appearing (the pipes-held-open path). **Do not** write it as "first
    `wait_for` the whole timeout, then look at returncode on timeout" -- that would
    wait a full timeout for nothing when the pipes are held open, while measured the
    answer is available after 0.25 seconds.

    Two phases: first give it `timeout` seconds to leave properly on its own (the
    normal path is over within milliseconds), kill only if it has not gone, then
    give `timeout` more seconds to reap the body. So the worst case is 2×timeout,
    still bounded.

    On the way out `wait()` is always cancelled + gathered, otherwise "stuck
    forever" would merely be traded for "an orphaned task".

    `timeout=None` -> the module constant is looked up **at call time** (do not
    write it as a default argument: a default argument is fixed when the `def`
    runs, so later changes to the module constant would have no effect and tests
    could not swap the value).
    """
    if timeout is None:
        timeout = _DOROSSI_REAP_TIMEOUT_SEC
    if proc.returncode is not None:
        return proc.returncode
    wait_task = asyncio.ensure_future(proc.wait())
    try:
        for phase in (0, 1):
            deadline = time.monotonic() + timeout
            while True:
                if proc.returncode is not None:
                    # The process is already dead, only the pipes are still held by
                    # someone else -> the rc is available, no need to wait more.
                    return proc.returncode
                if wait_task.done():
                    try:
                        return wait_task.result()
                    except Exception:  # pylint: disable=broad-except
                        return proc.returncode
                if time.monotonic() >= deadline:
                    break
                # `asyncio.wait` rather than `sleep`: it returns as soon as `wait()`
                # completes (zero extra delay on the normal path), while getting a
                # chance to glance back at `returncode` every poll interval.
                await asyncio.wait({wait_task}, timeout=_DOROSSI_REAP_POLL_SEC)
            if phase == 0:
                # Really still alive. This is what the "unexpected exit path" should
                # do: never leave a `full`-mode backend process running on the host.
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                except Exception:  # pylint: disable=broad-except  # nosec B110
                    pass
        return proc.returncode
    finally:
        wait_task.cancel()
        await asyncio.gather(wait_task, return_exceptions=True)


async def _dorossi_drain_stderr(task, timeout: float | None = None) -> str:
    """Reap the stderr-draining task and retrieve its content; when it cannot be
    had, **degrade to an empty string** rather than failing the whole round.

    The bound exists for the same reason as in `_dorossi_reap_proc`:
    `_read_stream_all` waits for EOF, and when EOF arrives is in someone else's
    hands. After a timeout it must be cancelled + gathered -- adding a bound
    without reaping the task merely trades "stuck forever" for "an orphaned task".

    stderr only feeds diagnostics (the third-ranked source of `failure_reason`,
    after the result event and the stdout tail), so carrying on to the verdict
    with an empty string when it cannot be had is the correct degradation, not
    swallowing an error.

    `timeout=None` means the same as in `_dorossi_reap_proc`: the module constant is
    looked up at call time.
    """
    if timeout is None:
        timeout = _DOROSSI_REAP_TIMEOUT_SEC
    if task is None:
        return ""
    try:
        raw = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return raw.decode("utf-8", "replace").strip()
    except (asyncio.TimeoutError, TimeoutError):
        return ""
    except Exception:  # pylint: disable=broad-except
        return ""
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def find_codex_executable() -> str | None:
    """Locate Codex even when a long-running supervisor has a stale PATH.

    `CODEX_CLI_PATH` is the explicit operator override.  On Windows the desktop
    installer uses LocalAppData, which is checked after the normal PATH lookup.
    Only existing regular files are returned; no shell wrapper is involved.

    Quote normalisation goes through `_dorossi_unquote_dir()`; do not write another
    copy here. Before 2026-09-10 this line was `.strip().strip('"')`, with two
    problems: it could only strip `"`, while `CODEX_CLI_PATH` is **an environment
    variable set from a shell**, where `CODEX_CLI_PATH='…/codex.exe'` is a perfectly
    normal spelling -- the single quotes stayed in the string,
    `Path(...).is_file()` was False, and so the thing this docstring calls "the
    explicit operator override" was **silently ignored**, falling back to the PATH
    search with no visible difference for the operator. The second problem is that
    it used exactly the "scrape every quote off both ends" style that
    `_dorossi_unquote_dir`'s docstring explicitly warns against (a path really named
    `'foo'` would be turned into a different path). The module already had the
    correct version, and this used to be the project's **third** private
    implementation of that rule.
    """
    override = _dorossi_unquote_dir(os.environ.get("CODEX_CLI_PATH", ""))
    candidates = [override, _shutil.which("codex")]
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            candidates.append(str(
                Path(local_app_data) / "Programs" / "OpenAI" / "Codex"
                / "bin" / "codex.exe"))
    for candidate in candidates:
        if candidate:
            try:
                path = Path(candidate).expanduser()
                if path.is_file():
                    return str(path.resolve())
            except OSError:
                continue
    return None


# ==========================================================================
# Daily model-catalogue check (2026-09-23)
# ==========================================================================
# Neither CLI has a "list models" subcommand (`claude --help` and
# `codex exec --help` checked on 2026-09-23). So there are only two ways to
# discover, ordered by cheapness:
#
#   1. **The SDK's model list** (`client.models.list()`) -- authoritative, the whole
#      catalogue in one go, but it needs credentials. This machine sets no
#      credential environment variables (measured the same day), so this does not
#      run here; it is kept because a host with credentials should take it.
#   2. **Probing** -- call the CLI once with a **bare alias** and read back **what it
#      resolved to**. That is "the newest of this family today", exactly the
#      answer we want.
#
# **Probing costs zero tokens, not "a few".** On the claude side the
# `system`/`init` event is printed **before** any request is sent, carrying the
# resolved full model id; the process is killed as soon as that line is read. The
# codex side is even simpler: it prints the header (including the `model:` line)
# first and **then** reads the prompt from stdin, so as long as not a single
# character is written to stdin, it is killed on reading the header and no request
# even takes shape. The daily cost is therefore 5 process launches (four claude
# families + one codex), a few seconds each.
#
# Failures only ever go to stderr, keep yesterday's catalogue and retry tomorrow --
# being offline, out of quota or missing the CLI should never be worse than "not
# updated today".
DOROSSI_MODEL_PROBE_TIMEOUT_SEC = 90.0
# The model line in the codex header (`model: gpt-5.6-sol`).
_DOROSSI_CODEX_MODEL_LINE_RE = re.compile(r"^\s*model:\s*(\S+)\s*$")


def _dorossi_model_probe_dir() -> Path:
    """An empty working directory for probing.

    Deliberately **neither** the repo root nor a session's directory: the CLI reads
    instruction files under the cwd, and probing from an empty directory is fast
    and keeps the probe out of every conversation's record. It sits under
    `DOROSSI_CC_WORKDIR`, so the existing gitignore entry covers it.
    """
    path = DOROSSI_CC_WORKDIR / ".model_probe"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _dorossi_model_from_init_line(raw: bytes) -> str | None:
    """One line of streaming JSON -> whether it is `system`/`init`, and if so the
    resolved full model id.

    Extracted as a pure function so it can be tested: the **entire judgement** of
    both probes rests on this one line, and starting a real CLI child process to
    test it is slow and needs the network. None has two meanings (not init /
    unreadable), and the caller "reads the next line" for both, so there is no need
    to tell them apart. Never raises -- what is fed in are bytes printed by another
    process.
    """
    try:
        event = _json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:  # pylint: disable=broad-except
        return None
    if not isinstance(event, dict):
        return None
    if event.get("type") != "system" or event.get("subtype") != "init":
        return None
    model = event.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None


def _dorossi_model_from_header_line(raw: bytes) -> str | None:
    """One header line of the other CLI -> what `model:` prints (None if it is not
    that line). Never raises."""
    match = _DOROSSI_CODEX_MODEL_LINE_RE.match(raw.decode("utf-8", errors="replace"))
    return match.group(1) if match else None


def _dorossi_model_probe_argv(exe: str, family: str) -> list:
    """The argv for the claude probe. Pure function.

    **Deliberately does not share `_dorossi_cc_argv`**, for two reasons, both about
    safety rather than tidiness: that one adds
    `--permission-mode bypassPermissions` when `dorossi_cc_tools == "full"`, and a
    child process with full permissions just to read one init line has no reason
    to exist; it also attaches the whole system prompt, making "kill it after the
    first line" needlessly heavy. Only three things are needed here: streaming JSON
    (for the init event), a bare alias (to see the resolution), and zero tools,
    zero MCP (to start fast).
    """
    return [exe, "-p",
            "--output-format", "stream-json", "--verbose",
            "--model", family,
            # The host's global MCP config makes `claude -p` hang for minutes on the
            # cold-start health check.
            "--strict-mcp-config",
            # A tool allowlist, and an empty one (fail-closed). The probe needs no
            # tools.
            "--tools", ""]


async def _dorossi_probe_claude_family(exe: str, family: str, cwd: str,
                                       timeout_sec: float) -> str | None:
    """Call `claude -p --model <bare alias>` once and read back the resolved full
    model id from the init event.

    The process is killed the moment it is read -- init comes before the request,
    so this trip costs zero tokens. Any failure returns None.
    """
    args = _dorossi_model_probe_argv(exe, family)
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=cwd, env=_dorossi_cc_child_env(timeout_sec),
        limit=1024 * 1024)
    try:
        # `-p` must have a prompt (an empty stdin is treated as a usage error), but
        # we are done before it is ever sent.
        try:
            proc.stdin.write(b"ping\n")
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        deadline = time.monotonic() + timeout_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            raw = await asyncio.wait_for(proc.stdout.readline(), remaining)
            if not raw:
                return None
            model = _dorossi_model_from_init_line(raw)
            if model:
                return model
    except (asyncio.TimeoutError, TimeoutError):
        print(f"[dorossi] model probe timed out for family {family!r}",
              file=sys.stderr)
        return None
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] model probe failed for family {family!r}: {exc!r}",
              file=sys.stderr)
        return None
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        await _dorossi_reap_proc(proc)


async def _dorossi_probe_codex_default(exe: str, cwd: str,
                                       timeout_sec: float) -> str | None:
    """Call `codex exec` once and read back what the header's `model:` line prints.

    This is the only discovery channel on the codex side -- nothing in its `--json`
    stream mentions the model (measured 2026-09-23), and an unrecognised model name
    does not fall back to the default, it is a server 400. So `--json` is
    **deliberately not passed**: the header is printed only in the human-readable
    format.

    ⚠️ Both of these were learned only by measuring; they are written here so the
    next person does not trip over them again:

      1. **The prompt must be written to stdin first.** The CLI reads stdin to the
         end before printing the header, and without it waits all the way to the
         timeout (the first version deliberately wrote nothing, hoping not to send
         even a prompt; measured, it used up the full 90 seconds).
      2. **The header is printed on stderr, not stdout.** In a terminal `2>&1` hides
         the difference, so the first version read stdout and got only the one line
         of answer after the whole round had run -- in effect running a whole round
         every day for nothing. Reading stderr instead, `model:` arrives before the
         request is sent, the process can simply be killed, and it costs zero
         tokens.
    """
    args = [exe, "exec", "--skip-git-repo-check", "-C", str(cwd),
            "-c", 'sandbox_mode="read-only"', "-"]
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd), limit=1024 * 1024)
    try:
        try:
            proc.stdin.write(b"ping\n")
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        deadline = time.monotonic() + timeout_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            raw = await asyncio.wait_for(proc.stderr.readline(), remaining)
            if not raw:
                return None
            model = _dorossi_model_from_header_line(raw)
            if model:
                return model
    except (asyncio.TimeoutError, TimeoutError):
        print("[dorossi] model probe timed out for the other backend",
              file=sys.stderr)
        return None
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] model probe failed for the other backend: {exc!r}",
              file=sys.stderr)
        return None
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        await _dorossi_reap_proc(proc)


def _dorossi_api_credentials_present() -> bool:
    """Whether the host has credentials the SDK can use. **Only checks presence;
    never prints the value.**"""
    return any(str(os.environ.get(name) or "").strip()
               for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))


async def _dorossi_probe_api_catalog(timeout_sec: float) -> dict:
    """With credentials, use the SDK's model list: the whole catalogue in one go,
    cheaper and more complete than probing.

    Returns `{family: full id}`, keeping only the families in the built-in table,
    each taking the newest entry in the list (the SDK lists newest first). No
    credentials / no SDK / any failure returns an empty dict, and the caller falls
    back to probing.
    """
    if not _dorossi_api_credentials_present():
        return {}
    cli = _get_dorossi_client()
    if cli is None:
        return {}
    try:
        page = await asyncio.wait_for(cli.models.list(limit=100), timeout_sec)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] model list unavailable: {type(exc).__name__}",
              file=sys.stderr)
        return {}
    newest: dict = {}
    for item in (getattr(page, "data", None) or []):
        model_id = getattr(item, "id", None)
        family, _version = _dorossi_split_model_id(model_id)
        if family in DOROSSI_MODEL_PROBE_FAMILIES and family not in newest:
            newest[family] = str(model_id)
    return newest


async def dorossi_probe_model_catalog(
        timeout_sec: float | None = None) -> dict:
    """Run discovery once (no file writes, no table changes), returning
    `{namespace: {family: full id}}`.

    Extracted so "discovery" and "landing" can be tested separately: this one
    touches the outside world, the one below touches only the file and the two
    tables. **It does raise**: an individual probe reading nothing is swallowed
    inside, but the `OSError` from creating the probe directory or starting a child
    process (the executable vanished after `which`, insufficient permissions)
    propagates and is caught by `dorossi_refresh_model_catalog`.
    """
    if timeout_sec is None:
        timeout_sec = DOROSSI_MODEL_PROBE_TIMEOUT_SEC
    resolved: dict = {}
    claude_found = await _dorossi_probe_api_catalog(timeout_sec)
    exe = _shutil.which("claude")
    if not claude_found and exe:
        cwd = str(_dorossi_model_probe_dir())
        for family in DOROSSI_MODEL_PROBE_FAMILIES:
            model_id = await _dorossi_probe_claude_family(
                exe, family, cwd, timeout_sec)
            if model_id:
                claude_found[family] = model_id
    if claude_found:
        resolved["claude"] = claude_found
    codex_exe = find_codex_executable()
    if codex_exe:
        model_id = await _dorossi_probe_codex_default(
            codex_exe, str(_dorossi_model_probe_dir()), timeout_sec)
        family, _version = _dorossi_split_model_id(model_id)
        if family and model_id:
            resolved["codex"] = {family: str(model_id)}
    return resolved


def dorossi_model_catalog_due(interval_hours: float,
                              now: float | None = None) -> bool:
    """Has it been long enough since the last check? The last time is stored in the
    catalogue file, so **a restart does not rerun it**.

    A stored time later than now (the clock was adjusted, the file was copied from
    another machine) is always treated as due -- otherwise a future time would
    switch this check off for good, with no symptom at all. Likewise `NaN`:
    `json.loads` accepts it, and it compares False against any number, so every
    comparison below would miss.
    """
    now = time.time() if now is None else now
    last = _DOROSSI_MODEL_CATALOG.get("checked_at")
    if (not isinstance(last, (int, float)) or not math.isfinite(last)
            or last <= 0 or last > now):
        return True
    return (now - last) >= max(0.0, float(interval_hours)) * 3600.0


async def dorossi_refresh_model_catalog(
        timeout_sec: float | None = None) -> list:
    """Discover -> land -> merge back into the tables, returning **the aliases added
    this time** (possibly empty).

    The return value is material for an announcement, so it is only ever aliases;
    not a single character of a full id is carried out of this layer. When every
    probe fails the stored catalogue is **left untouched** (keep yesterday's
    results, retry tomorrow); on partial success only the families that succeeded
    are updated. Never raises.

    A probe raising is also treated as "every probe failed", and **`checked_at` is
    still stamped**. Originally this path returned straight away without landing
    anything, and the throttle looks exactly at `checked_at`, so a day when child
    processes could not start turned into a retry every minute -- each printing a
    stderr line and each holding up the health loop until it failed.
    """
    global _DOROSSI_MODEL_CATALOG  # pylint: disable=global-statement
    try:
        resolved = await dorossi_probe_model_catalog(timeout_sec)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] model catalog probe failed: {exc!r}", file=sys.stderr)
        resolved = {}
    merged = {}
    previous = _DOROSSI_MODEL_CATALOG.get("resolved")
    if isinstance(previous, dict):
        for namespace, found in previous.items():
            if isinstance(found, dict):
                merged[namespace] = dict(found)
    for namespace, found in resolved.items():
        merged.setdefault(namespace, {}).update(found)
    catalog = {
        "schema": DOROSSI_MODEL_CATALOG_SCHEMA,
        "checked_at": time.time(),
        "resolved": merged,
    }
    dorossi_save_model_catalog(catalog)
    _DOROSSI_MODEL_CATALOG = catalog
    return dorossi_merge_model_catalog(catalog)


_DOROSSI_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})

# The output root of the backend's image-generation tool. **A module-level
# constant is deliberate**: the paths `_collect_codex_images` produces are recorded
# by the bot in `recent_image_msgs.json`, and when the bot reloads that file it must
# judge "does this string fall under the allowed root" -- and that containment
# check uses exactly the same root. Writing the literal once on each side is "one
# rule, two implementations", one of which will drift sooner or later, and the
# symptom of drifting is **silent**: legitimate mappings are quietly dropped on the
# next start, and 🗑️ / ⭐ stop working with no error at all.
# It is deliberately **not** `.resolve()`d here -- each consumer resolves on its own
# side, so both junction / symlink and case differences are absorbed.
CODEX_IMAGE_ROOT = Path.home() / ".codex" / "generated_images"


def _collect_codex_images(thread_id: str | None, since_ns: int, *,
                          root: Path | None = None) -> list[str]:
    """Return images generated in this invocation for one Codex thread.

    Codex's image tool writes under ``~/.codex/generated_images/<thread-id>``.
    Restricting discovery to that exact directory and to new regular image
    files prevents arbitrary paths mentioned by model text from becoming
    Discord attachments.
    """
    if not isinstance(thread_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", thread_id):
        return []
    # `root=` is the test injection point and stays; without injection it uses the
    # single-source module-level constant.
    base = (root or CODEX_IMAGE_ROOT).resolve()
    folder = (base / thread_id).resolve()
    if base not in folder.parents or not folder.is_dir():
        return []
    found: list[tuple[int, str]] = []
    try:
        for path in folder.iterdir():
            if (not path.is_file()
                    or path.suffix.lower() not in _DOROSSI_IMAGE_SUFFIXES):
                continue
            stat = path.stat()
            if stat.st_mtime_ns >= since_ns:
                found.append((stat.st_mtime_ns, str(path)))
    except OSError:
        return []
    found.sort()
    return [path for _mtime, path in found]


# --- Two defensive accessors for folding events -------------------------------
#
# Both `feed` docstrings promise "**never raises**", and that promise is
# load-bearing: the claude side's read loop has **no** try/finally, so an exception
# would escape the whole function -> nobody kills the child process, the
# stderr-draining task is orphaned, that session's lock is stuck for good, and the
# unattended loop stops on the spot with no signal at all.
#
# On 2026-09-08, every event skeleton and every nested position of both feeds was
# swept with every unexpected value JSON can bring in (`None` / numbers / strings
# / `[]` / `[1]` / `true` / `{}` / decimals), uncovering **three** independent
# mechanisms. Fixing one spot at a time is exactly why this bug class keeps coming
# back in place, so these two helpers exist to block the whole class at once --
# use them when adding fields, and do not write `X or {}` again or drop external
# values straight into a set.
#
#   (A) Calling `.get()` on a non-dict. The idiom `X or {}` only stops **falsy**
#       values (`None`/`{}`), not **truthy non-dicts** (`5`, `"a string"`, `[1]`)
#       -> AttributeError.
#       -> Always go through `_event_dict()`.
#   (B) Putting an unhashable value in a set. JSON arrays/objects become
#       `list`/`dict`, and both `set.add()` **and `set.discard()`** raise
#       TypeError.
#       -> Always go through `_protocol_key()`.
#   (C) The input itself is not a str. `json.loads(None)` raises **TypeError**, not
#       ValueError.
#       -> The parse's except catches `(ValueError, TypeError)` together (see both
#       feeds).


def _event_dict(obj: dict, key: str) -> dict:
    """`obj[key]`, **or `{}` when it is not a dict**. Replaces
    `obj.get(key) or {}` (mechanism A)."""
    value = obj.get(key)
    return value if isinstance(value, dict) else {}


def _protocol_key(value):
    """A protocol identifier that is safe to use as a set element, or `None` when
    unrecognised (the caller drops it).

    Only `str` and `int` are accepted -- exactly the types the protocol itself uses
    (a content block's `index` is an integer, a `tool_use`'s `id` is a string).
    **Dropped rather than stringified**, for two reasons:

    * Conversion **invents data**. `str([1])` produces `"[1]"`, a ghost key that no
      **well-formed** later event will ever match; an unmatchable entry left in
      `pending_tools` would keep suppressing the idle watchdog (though the hard
      limit still backstops it), while dropping fails in the direction "the
      watchdog may fire a bit early" -- bounded, and failing loudly with a
      diagnostic. **Quietly suppressing a guard is far worse than loudly firing a
      bit early.**
    * `add` and `discard` use the same filter, so both sides agree: an unrecognised
      id never gets in, so it never needs removing.

    **`bool` is excluded** (`isinstance(True, int)` is True, so it must be spelled
    out): `index: true` would put `True` into `text_block_indices`, and
    `1 in {True}` holds -- so the delta of the **tool** block with index 1 would be
    treated as a known text block. That is the progress preview allowlist's safety
    property bypassed by aliasing, not mere type fastidiousness. Likewise this
    filter also blocks `None` (what `.get()` returns when the field is missing),
    otherwise "a text block missing its index" and "a tool block missing its index"
    would alias each other.
    """
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (str, int)) else None


class _CodexStreamState:
    """The accumulated state of one `codex exec --json` stream. Split out for the
    same reason as `_ClaudeStreamState`: the reading half is tied to the subprocess
    + watchdog, while this folding half is a pure data transformation."""

    def __init__(self, session_id: str | None = None) -> None:
        self.thread_id = session_id
        self.answer = ""
        self.usage: dict = {}
        # Text from failure events, used to classify rc!=0 (see
        # `_codex_stream_verdict`).
        self.failure_texts: list = []

    def feed(self, raw_line: str, on_text=None) -> None:
        """Fold one raw stdout line into the state. **Never raises**, and does no
        I/O.

        The same choice as the Claude side: an unparseable line is **skipped and
        reading continues**, without aborting the round.
        """
        try:
            event = _json.loads(raw_line)
        except (ValueError, TypeError):
            return  # non-JSON noise, or `raw_line` is not even str/bytes (mechanism C)
        if not isinstance(event, dict):
            # Legal JSON but not an object (`null` / a number / an array): the old
            # version raised AttributeError at `event.get(...)`, handing the caller
            # an untyped exception (abort / resume / usage limit all failed to
            # classify it). Treated the same as noise.
            return
        etype = event.get("type")
        if etype == "thread.started":
            self.thread_id = event.get("thread_id") or self.thread_id
        elif etype == "item.completed":
            item = _event_dict(event, "item")
            if item.get("type") == "agent_message" and item.get("text"):
                self.answer = str(item["text"]).strip()
                if on_text is not None:
                    try:
                        on_text(self.answer)
                    except Exception:  # pylint: disable=broad-except  # nosec B110
                        pass  # a failed progress update never affects the main stream
        elif etype == "turn.completed":
            self.usage = _event_dict(event, "usage")
        else:
            # Keep the text of failure-type events. codex's usage-limit / server
            # error messages do not necessarily appear on stderr (with JSON events
            # they are often only in the event), and the rc!=0 classification relies
            # on this text -- without it, it falls back to the old behaviour:
            # reopening a new session for nothing, then declaring the whole
            # unattended task dead. Event type names change between codex versions
            # (0.145.0 locally), so the type is **not hard-coded** here; any event
            # carrying an error / message / text field is kept -- better to keep too
            # much than to miss one.
            for key in ("error", "message", "text", "reason"):
                val = event.get(key)
                if isinstance(val, dict):
                    val = val.get("message") or val.get("text")
                if isinstance(val, str) and val.strip():
                    self.failure_texts.append(val.strip()[:500])
                    break


def _codex_stream_verdict(state: "_CodexStreamState", rc: int, err_text: str,
                          session_id: str | None) -> str:
    """Judge a `codex exec` stream that has **already ended**: return `"ok"` or
    raise.

    The classification order is exactly the same as the Claude side, and **the
    order is the rule itself**:
      usage limit → transient failure → cannot reach the server → (only last) the
      resume retry that throws the session away and reopens it.
    The resume retry does nothing at all for "quota used up" or "server
    overloaded"; it only burns another call and throws away a conversation that
    could have been resumed; before 2026-09-05 the codex side had **only** that
    path.

    The verdict text takes both stderr and JSON failure events: when codex emits
    events, the limit message often appears only in the event, with stderr empty.
    """
    if rc == 0:
        return "ok"
    print(f"[dorossi] codex exited rc={rc}: {err_text[:1000]}", file=sys.stderr)
    blob = "\n".join([err_text] + list(state.failure_texts))
    usage_exc = _dorossi_codex_usage_limit(blob, state.thread_id)
    if usage_exc is not None:
        raise usage_exc
    transient_exc = _dorossi_codex_transient(blob, state.thread_id)
    if transient_exc is not None:
        raise transient_exc
    offline_exc = _dorossi_codex_offline(blob, session_id)
    if offline_exc is not None:
        raise offline_exc
    if session_id:
        raise _DorossiResumeError("codex resume failed")
    raise RuntimeError("codex invocation failed")


def _dorossi_codex_argv(exe: str, *, session_id: str | None = None,
                        workdir: str | None = None,
                        extra_dir: str | None = None,
                        model: str | None = None,
                        tools_mode: str | None = None) -> list:
    """The argv (including `exe`) for `codex exec`. Pure function: touches no
    files, starts no process, reads no environment.

    Extracted from `_dorossi_via_codex` (2026-09-23, behaviour unchanged -- apart
    from the newly added `-m`), for the same reason as `_dorossi_cc_argv`: the argv
    is the only place on this path where "user input could become a CLI argument",
    and only as a pure function can every combination be tested.

    `model` is the **full model id** the `dorossi_resolve_model` lookup hit; None =
    no `-m`, use the CLI's own default. **User input never reaches here
    verbatim** -- every possible value of this argument comes from the allowlist
    lookup's result.

    When `tools_mode` is omitted the module global `DOROSSI_CC_TOOLS` is read **at
    call time** (as in `_dorossi_cc_argv`). The criterion is still "unlocks only
    when exactly `full`"; anything else reasserts the read-only sandbox.
    """
    if tools_mode is None:
        tools_mode = DOROSSI_CC_TOOLS
    args = [exe, "exec"]
    if session_id:
        args += ["resume", "--json"]
    else:
        args += ["--json", "--skip-git-repo-check", "-C", str(workdir or "")]
        if extra_dir:
            args += ["--add-dir", extra_dir]
    if model:
        # The per-round `/model` override. Both `exec` and `exec resume` accept
        # `-m/--model` (on 2026-09-23, both subcommands' help in CLI 0.145.0 listed
        # it). Without it, the CLI default applies.
        args += ["-m", model]
    if tools_mode == "full":
        args.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        # `exec resume` does not expose `--sandbox`, but it does accept config
        # overrides. Reassert read-only on every invocation rather than relying
        # on whatever the host's user config happens to contain.
        args += ["-c", 'sandbox_mode="read-only"']
    args += ([session_id, "-"] if session_id else ["-"])
    return args


async def _dorossi_via_codex(
        prompt: str, session_id: str | None, on_text=None,
        extra_dir: str | None = None, workdir: str | None = None,
        on_proc=None, abort_check=None, silence_limit: float | None = None,
        loop_system_guidance: str | None = None,
        model: str | None = None) -> tuple:
    """Run one non-interactive Codex turn; return answer, thread id and usage.

    `model` = the full model id parsed from this round's `/model` (None = no
    flag). Before 2026-09-23 this path **passed no `-m` at all**, so `/model`
    silently did nothing for codex."""
    exe = find_codex_executable()
    if exe is None:
        raise FileNotFoundError("codex CLI not found on PATH")
    effective_cwd = workdir or str(DOROSSI_CC_WORKDIR)
    try:
        cwd_path = Path(effective_cwd)
        # Create the directory automatically only inside the subtree we manage;
        # the criterion is a single decision point, so do not write another
        # `.parents` comparison here.
        if _dorossi_cwd_is_managed(cwd_path):
            cwd_path.mkdir(parents=True, exist_ok=True)
    except Exception:  # pylint: disable=broad-except
        pass
    # The stored directory may be long gone: say so clearly before spawning,
    # rather than letting the child raise a NotADirectoryError that would be judged
    # "CLI unusable". See `_dorossi_require_workdir`.
    _dorossi_require_workdir(effective_cwd)

    # The argv is built by `_dorossi_codex_argv` (a pure function); **do not write
    # another copy here**.
    args = _dorossi_codex_argv(
        exe, session_id=session_id, workdir=effective_cwd,
        extra_dir=extra_dir, model=model)
    turn_prompt = prompt
    if loop_system_guidance:
        turn_prompt += "\n\n" + loop_system_guidance
    wire_prompt = turn_prompt if session_id else (
        DOROSSI_SYSTEM_PROMPT + "\n\n使用者問題：\n" + turn_prompt)

    invocation_started_ns = time.time_ns()
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=effective_cwd,
        limit=16 * 1024 * 1024)
    # Callbacks / stdin are always wrapped (in line with _dorossi_via_claude_code):
    # these are all "auxiliary actions", and their failure must never take the whole
    # round down. In particular, when abort_check hits, the process is killed first
    # and the stdin write right after is bound to BrokenPipe -- if not swallowed,
    # the exception would escape "outside" the try/finally below, stderr_task would
    # become an orphaned task nobody awaits, and the caller would get an untyped
    # exception (the abort path should go kill -> EOF -> rc!=0).
    if on_proc is not None:
        try:
            on_proc(proc)
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
    if abort_check is not None:
        try:
            if abort_check():
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
    stderr_task = asyncio.create_task(_read_stream_all(proc.stderr))
    try:
        proc.stdin.write(wire_prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
    except Exception:  # pylint: disable=broad-except
        pass
    state = _CodexStreamState(session_id)
    clock = _DorossiWatchClock()        # minus host sleep time (see `_dorossi_readline_watched`)
    deadline = clock.now() + _dorossi_cc_hard_limit_sec()
    try:
        while True:
            timeout = (silence_limit if silence_limit is not None
                       else deadline - clock.now())
            if timeout <= 0:
                raise TimeoutError("codex hard limit exceeded")
            raw = await _dorossi_readline_watched(proc.stdout, timeout, clock)
            if not raw:
                break
            state.feed(raw.decode("utf-8", errors="replace"), on_text)
        wait_timeout = (silence_limit if silence_limit is not None
                        else max(1.0, deadline - clock.now()))
        rc = await asyncio.wait_for(proc.wait(), timeout=wait_timeout)
    except (asyncio.TimeoutError, TimeoutError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        # Reap with a bound (no bare `await proc.wait()`): if the pipes are still
        # held by a grandchild after the kill, `wait()` only returns at EOF, i.e.
        # never -- see `_dorossi_reap_proc`.
        await _dorossi_reap_proc(proc)
        if silence_limit is not None:
            raise _DorossiLoopSilence("codex loop output silence")
        raise TimeoutError("codex invocation timed out")
    finally:
        # Make sure the child has really ended before awaiting stderr_task.
        # `_read_stream_all` returns only at EOF, and EOF only happens after the
        # child ends (pipes closed) -- so on any "unexpected" exit path (a callback
        # raising, the task cancelled, readline exceeding the buffer limit) that
        # leaves the process alive, awaiting it directly would **hang forever**,
        # outside wait_for with no watchdog guarding it, which amounts to holding
        # that session's lock hung for good. On the normal path (rc obtained, or
        # killed above) returncode is already set and this is a no-op.
        #
        # **Correction 2026-09-09: "kill first, then await" alone is not enough.**
        # Measured (CPython 3.14 / Windows): `await proc.wait()` is itself unbounded
        # -- it waits for **every pipe to reach EOF**, and `proc.kill()` is
        # `TerminateProcess`, which cannot take along a grandchild that inherited
        # the pipes. So both the wait after the kill and the stderr draining **need
        # a bound**, and the tasks are reaped cleanly after a timeout.
        # The synchronous kill stays here (do not move it into
        # `_dorossi_reap_proc`'s "wait, then kill" order): on an unexpected exit
        # path we are not guaranteed a chance to finish any await.
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            except Exception:  # pylint: disable=broad-except  # nosec B110
                pass
        await _dorossi_reap_proc(proc)
        err_text = await _dorossi_drain_stderr(stderr_task)
    _codex_stream_verdict(state, rc, err_text, session_id)
    return state.answer, state.thread_id, {
        "usage": state.usage,
        "images": _collect_codex_images(state.thread_id, invocation_started_ns),
    }


class _ClaudeStreamState:
    """The accumulated state of one `claude -p --output-format stream-json` stream.

    **Why it is split from the invocation**: the reading half is tied to the
    subprocess + the two-stage watchdog + `proc.kill()`, and testing it means
    really starting a backend; this folding half is purely an "events -> state"
    data transformation, and once extracted it can be fed a fake stream. All the
    data the error classification needs (usage limit / transient overload /
    output silence) is accumulated here, and those three paths are exactly the
    scene of the 2026-09-05 incident -- the verdict itself is in
    `_claude_stream_verdict`.

    `kill_reason` is set by the **reading side** (when a watchdog kills the
    process), not by the folding side: the folding side cannot see time, and should
    not. None means "the stream reached EOF by itself" -- note that this is **not
    the same as** success; a killed backend or a broken pipe is EOF too, and what
    tells them apart is the rc (see `_claude_stream_verdict`).
    """

    _STDOUT_TAIL_LINES = 25

    def __init__(self, session_id: str | None = None) -> None:
        self.sid = session_id
        self.answer = ""
        # Streaming-progress (single-message live preview) state: accumulates the
        # text_delta of the "answer text". Only type=="text" content blocks are
        # accumulated; a tool_use's input_json_delta does not count and is never
        # sent to Discord -- progress reflects only the sanitised answer text.
        self.stream_text = ""
        self.text_block_indices: set = set()   # content block indices known to be text
        self.pending_tools: set = set()        # tool_use ids issued but with no result yet
        # The background tasks the CLI currently reports (task_ids of
        # `system`/`background_tasks_changed`). Like `pending_tools`, a suppressing
        # condition for the idle watchdog: the model can leave a not-yet-triggered
        # watch task at the end of a turn, the CLI sends `result` first, then waits
        # **in complete silence** for it to trigger, calls the model again and sends
        # a second `result`. A background task is not a pending tool_use, so
        # looking only at `pending_tools` would cut that wait as idle (the
        # 2026-09-19 incident).
        self.background_tasks: set = set()
        # In `--output-format stream-json` mode, a failing `claude -p` writes the
        # error into a `result` event on STDOUT (not stderr). Keep the last result
        # event object and a bounded stdout tail, to diagnose the real cause when
        # rc != 0.
        self.last_result_ev: dict = {}
        self.stdout_tail: list = []
        # The reset time (epoch seconds) given by this call's last
        # `rate_limit_event`. On a usage-limit hit it decides how long to sleep --
        # the only structured source, straight from the server's quota headers.
        self.rate_reset: float | None = None
        # The same event's status (allowed / allowed_warning / rejected). It has
        # only one use: vetoing the text verdict in a successful round (see
        # `_dorossi_cc_limit_text_counts`).
        self.rate_status: str | None = None
        # The init event's `claude_code_version`. Its only use: judging whether this
        # `--resume`'s reported `total_cost_usd` / `modelUsage` are per invocation or
        # session-cumulative (the latter from 2.1.277 on, see
        # `_dorossi_cc_account_round`). The bot starts the CLI afresh every round and
        # the CLI auto-updates, so the version must be read from the stream on
        # **every invocation**, not asked once when the bot starts.
        self.cli_version: str | None = None
        # Whether the init event has the `memory_paths` key (None = no init seen
        # yet). Measured (2.1.276): present in normal mode under both tool modes,
        # absent entirely with `--bare` / `CLAUDE_CODE_SIMPLE=1` -- the only visible
        # structured signal that "the CLI read no sign-in and no instruction files"
        # (`apiKeySource` is "none" in both bare and normal mode and cannot tell
        # them apart). Used by the start-up warning and the not-logged-in
        # diagnostic line.
        self.init_memory_paths: bool | None = None
        # The init event's `apiKeySource` (the CLI's source **label**, e.g. "none" /
        # "ANTHROPIC_API_KEY", not the key itself). "none" = using the sign-in; any
        # other value = billing through an API key instead. Used only by the
        # start-up warning, and it must pass `_DOROSSI_API_KEY_SOURCE_LABEL_RE`
        # before being printed.
        self.api_key_source: str | None = None
        # The error class and status code of the last `system`/`api_retry` (the
        # CLI's own classification; vocabulary above `_DOROSSI_AUTH_RETRY_ERRORS`).
        # **Overwritten by every one**: a later non-auth retry must be able to
        # override an earlier auth one, otherwise one earlier transient 401 would
        # get the whole round judged not-logged-in.
        self.retry_error: str | None = None
        self.retry_status: int | None = None
        self.kill_reason: str | None = None    # None / "idle" / "hard" / "silence"

    @staticmethod
    def _content_blocks(ev: dict) -> list:
        """`ev["message"]["content"]`; any shape other than a list returns `[]`
        (mechanisms A/B).

        The default of `ev.get("message", {})` **only applies when the key is
        missing** -- with the key present and the value `null` it is not used, so
        `.get("content")` raises AttributeError; with `content` a number,
        `for blk in 5` raises TypeError. Both break `feed`'s "never raises" promise.
        """
        blocks = _event_dict(ev, "message").get("content")
        return blocks if isinstance(blocks, list) else []

    @staticmethod
    def _background_task_ids(tasks) -> set:
        """The `tasks` snapshot of `background_tasks_changed` -> a set of task_ids
        (mechanisms A/B).

        That event carries a **complete snapshot**, not an increment, so the caller
        replaces the old set entirely. A wrong shape (`tasks` not a list, an item not
        a dict, `task_id` unhashable or missing) is always dropped, **not kept as the
        old value**: for the same reason as `_protocol_key` -- an unrecognised
        snapshot that left the old set in place would quietly keep suppressing the
        idle watchdog; dropping fails in the direction "the watchdog may fire a bit
        early", bounded and loud (`_claude_stream_verdict` still keeps the answer
        when a successful one is already in hand).
        """
        if not isinstance(tasks, list):
            return set()
        ids = set()
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = _protocol_key(task.get("task_id"))
            if task_id:
                ids.add(task_id)
        return ids

    def _keep_in_tail(self, raw_line) -> None:
        """Record a line in the bounded stdout tail (the second-ranked source of
        `failure_reason`). Empty lines are not recorded."""
        if raw_line:
            self.stdout_tail.append(raw_line)
            if len(self.stdout_tail) > self._STDOUT_TAIL_LINES:
                del self.stdout_tail[0]

    def feed(self, raw_line: str, on_text=None) -> None:
        """Fold one raw stdout line into the state. **Never raises**, and does no
        I/O.

        A bad line is **skipped and reading continues**, rather than aborting the
        round: non-JSON noise mixed into the stream (CLI warnings, truncated half
        lines) is normal, and throwing away a whole round's finished work over one
        line of noise costs far more. The real failure signals are the rc and the
        `result` event, not one line failing to parse.
        """
        try:
            ev = _json.loads(raw_line)
        except (ValueError, TypeError):
            # ValueError = non-JSON noise (including empty lines and truncated half
            #              lines).
            # TypeError  = `raw_line` is not even str/bytes (mechanism C):
            #              `json.loads(None)` raises TypeError, and catching only
            #              ValueError would let it escape.
            self._keep_in_tail(raw_line)
            return
        # Per-token `stream_event`s stay out of the diagnostic tail. There are
        # hundreds of them per round, and they would fill the 25-line tail, pushing
        # out the lines that actually explain a failure (non-JSON errors, `system` /
        # `assistant` events); and without a `result`, this tail is exactly what
        # `failure_reason` prints.
        if not (isinstance(ev, dict) and ev.get("type") == "stream_event"):
            self._keep_in_tail(raw_line)
        if not isinstance(ev, dict):
            # Legal JSON but not an object (`null` / a number / an array). The old
            # version called `ev.get(...)` directly, which raises AttributeError, and
            # the claude side's read loop has **no** try/finally -- the exception
            # would escape the whole function, nobody would reap the child or the
            # stderr-draining task, and a leaked process would still hold that
            # session's lock. Treated the same as noise.
            return
        etype = ev.get("type")
        if etype == "stream_event":
            # Per-token streaming from --include-partial-messages. Only the "answer
            # text" text_delta (content blocks with type=="text") is accumulated for
            # on_text; a tool_use's input_json_delta is not, so progress never leaks
            # tool-call content.
            sev = _event_dict(ev, "event")
            stype = sev.get("type")
            # Both branches key on this index. `None` means "an unrecognised index"
            # -- **dropped on both sides**, so registering and matching are always
            # symmetric (see the aliasing note on `_protocol_key`).
            index = _protocol_key(sev.get("index"))
            if stype == "content_block_start":
                blk = _event_dict(sev, "content_block")
                if blk.get("type") == "text" and index is not None:
                    self.text_block_indices.add(index)
            elif stype == "content_block_delta":
                delta = _event_dict(sev, "delta")
                if delta.get("type") == "text_delta" \
                        and index is not None \
                        and index in self.text_block_indices:
                    piece = delta.get("text") or ""
                    if piece:
                        self.stream_text += piece
                        if on_text is not None:
                            try:
                                on_text(self.stream_text)
                            except Exception:  # pylint: disable=broad-except  # nosec B110
                                pass  # a failed progress update never affects the main stream
            return
        if etype == "rate_limit_event":
            # One arrives on every call, carrying this quota window's real reset
            # time. Keep the last one: on a limit hit it can **sleep until that
            # moment** instead of the "start at 15 minutes, double each time" guess.
            # See `_dorossi_rate_limit_reset`.
            got = _dorossi_rate_limit_reset(ev)
            if got is not None:
                self.rate_reset = got
            # status is **overwritten by every event** (None when unreadable),
            # unlike the above which keeps the previous value: when the previous one
            # said allowed and this one is an unreadable refusal, keeping the old
            # allowed would veto a real notice. None fails in the direction "no
            # veto", falling back to the text verdict.
            self.rate_status = _dorossi_rate_limit_status(ev)
            return
        if etype == "system":
            if ev.get("session_id"):
                self.sid = ev["session_id"]
            # Real streams' system events all carry session_id, so this **must not**
            # be an elif of the branch above, or the background-task snapshot would
            # never be read.
            if ev.get("subtype") == "init":
                version = ev.get("claude_code_version")
                if isinstance(version, str) and version.strip():
                    self.cli_version = version.strip()[:64]
                # It looks at "is the key there", not the value: in bare mode the key
                # is absent entirely (see __init__).
                self.init_memory_paths = "memory_paths" in ev
                source = ev.get("apiKeySource")
                self.api_key_source = source[:64] if isinstance(source, str) else None
            if ev.get("subtype") == "api_retry":
                self.retry_error, self.retry_status = _dorossi_api_retry_fields(ev)
            if ev.get("subtype") == "background_tasks_changed":
                self.background_tasks = self._background_task_ids(ev.get("tasks"))
        elif etype == "assistant":
            for blk in self._content_blocks(ev):
                if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                    continue
                tool_id = _protocol_key(blk.get("id"))
                if tool_id:
                    self.pending_tools.add(tool_id)
        elif etype == "user":
            for blk in self._content_blocks(ev):
                if not isinstance(blk, dict) or blk.get("type") != "tool_result":
                    continue
                # `discard`, like `add`, raises TypeError on an unhashable value, so
                # the removal side must pass the same filter too -- and it **must be
                # the same one**, or something could be registered yet never removed.
                tool_id = _protocol_key(blk.get("tool_use_id"))
                if tool_id:
                    self.pending_tools.discard(tool_id)
        elif etype == "result":
            self.last_result_ev = ev  # keep the whole event (subtype/is_error/...) for diagnosis
            # Only a string counts as an answer. A non-string (`null` / a number / an
            # object) means this event has no usable answer text, not "the answer is
            # its repr" -- the raw event stays whole in `last_result_ev`, where the
            # classification (usage limit / budget gate) still sees it.
            raw_answer = ev.get("result")
            self.answer = raw_answer.strip() if isinstance(raw_answer, str) else ""
            if ev.get("session_id"):
                self.sid = ev["session_id"]

    def failure_reason(self, stderr_tail: str = "") -> str:
        """The diagnostic string assembled when rc != 0 (goes only to stderr, never
        to the chat platform).

        In stream-json mode `claude -p` writes errors into the result event on
        STDOUT rather than stderr, so the sources in order are: result event ->
        stdout tail -> stderr.
        """
        if self.last_result_ev:
            parts = []
            for key in ("subtype", "is_error", "api_error_status", "result"):
                val = self.last_result_ev.get(key)
                if val not in (None, ""):
                    parts.append(f"{key}={val}")
            reason = "; ".join(parts) if parts else ""
        else:
            reason = ""
        if not reason:
            # `str(x)`: since mechanism C was fixed, non-str lines can get into
            # `stdout_tail` too, and `join` raises TypeError on non-str elements --
            # the diagnostic path must not blow up either.
            reason = "\n".join(str(x) for x in self.stdout_tail)[-4000:]
        if not reason:
            reason = stderr_tail[-400:]
        if not reason:
            reason = "(no output)"
        return reason


# The line the CLI's command-line parser prints when it rejects an unrecognised
# option (measured 2026-09-19 by feeding 2.1.276 a nonexistent option:
# `error: unknown option '--tools-bogus-xyz'`, rc=1, stdout completely empty). The
# capture group is deliberately narrowed to "looks like a flag", so only a flag
# name can ever be printed into the log, never arbitrary text.
_CLI_UNKNOWN_OPTION_RE = re.compile(r"unknown option '(--?[A-Za-z0-9][A-Za-z0-9_-]{0,63})'")


def _dorossi_cc_rejected_option(state: "_ClaudeStreamState",
                                stderr_tail: str) -> str | None:
    """When rc != 0: did the CLI reject one of the options we passed **before
    starting the round**? If so return that option name, otherwise None. Pure
    function, never raises.

    Both conditions must hold: the stream has **no** `result` event at all (if it
    has one, the round already started and the failure has another cause -- those
    still go through the classifications further on), and stderr has the parser's
    line. Looking at stderr alone is not enough: a round that started normally,
    whose stderr happens to contain that string, must not be called "CLI too old"."""
    try:
        # "No result" on this state object is an **empty dict**, not None
        # (`__init__` sets it to `{}`, and `failure_reason` also tests truthiness) --
        # writing `is not None` would make this always return None.
        if state.last_result_ev:
            return None
        match = _CLI_UNKNOWN_OPTION_RE.search((stderr_tail or "")[-4000:])
        return match.group(1) if match else None
    except Exception:  # pylint: disable=broad-except
        return None


# ---- The backend CLI has no usable sign-in (added after measuring, 2026-09-19) ----
#
# Measured (CLI 2.1.276), three shapes; the first two are what this section
# recognises, the third is what the resume retry is **really** for:
#
#   | Situation | rc | Stream | result |
#   |---|---|---|---|
#   | `--bare` (or `CLAUDE_CODE_SIMPLE=1`), no key in the environment | 1 (1.1 s) | init (**no** `memory_paths`), assistant, result | `subtype` still "success", `is_error` true, `api_error_status` null, `terminal_reason` "api_error", text "Not logged in · Please run /login" |
#   | `--bare` + an invalid `ANTHROPIC_API_KEY` | 1 (**190 s**) | ten `system`/`api_retry` (`error` "authentication_failed", `error_status` 401), then assistant, result | `api_error_status` **401**, text "Failed to authenticate. API Error: 401 API key is invalid." |
#   | `--resume <nonexistent id>` | 1 | result | `subtype` "error_during_execution", `result` null, `errors` ["No conversation found …"] |
#
# The criteria are ranked by strength of evidence: **structured fields first,
# text last**, and the text must pass two more checks (the same lesson as
# `_dorossi_cc_limit_text_counts` -- broadly matching phrases may only be used on
# error messages, never on answers).
_DOROSSI_AUTH_STATUSES = frozenset({401})
# The `error` of `api_retry` is the CLI's own classification (the SDK schema in the
# 2.1.276 executable: authentication_failed, oauth_org_not_allowed, account_on_hold,
# verification_required, billing_error, rate_limit, overloaded, invalid_request,
# model_not_found, server_error, unknown, max_output_tokens, cloud_credential_error).
# The CLI itself marks five of these as "stuck, needs a human"; only the three
# **credential** ones are taken here -- the other two (account on hold,
# verification required) will not fix themselves either, but "sign in on the host"
# is the wrong prescription, so those two still take the capped retry.
# billing_error belongs to usage / payment and is handled by the usage-limit
# branch (402).
_DOROSSI_AUTH_RETRY_ERRORS = frozenset({
    "authentication_failed", "oauth_org_not_allowed", "cloud_credential_error"})
# The fallback when there is only text and no structured field (bare mode's "Not
# logged in" is exactly that: status code null, no api_retry). Matched
# lower-case.
_DOROSSI_AUTH_TEXT_MARKERS = ("not logged in", "please run /login", "failed to authenticate")
# The length cap for the text fallback. The CLI's notice is a one-line template
# (the two measured sentences are 33 and 58 characters); an answer that talks about
# /login is prose. Same cap as the usage-limit branch, for the same reason.
_DOROSSI_AUTH_NOTICE_MAX_CHARS = 300


def _dorossi_api_retry_fields(ev) -> tuple:
    """A `system`/`api_retry` event -> (error class, status code). Anything
    unrecognised is None. Never raises.

    The class is accepted only as a string and truncated (it is only used for
    membership tests, and what gets printed is the matched fixed vocabulary); the
    status code is accepted only as a real integer (`True` is a subclass of int
    and must be excluded)."""
    try:
        error = ev.get("error")
        status = ev.get("error_status")
    except Exception:  # pylint: disable=broad-except
        return None, None
    error = error[:64] if isinstance(error, str) else None
    if not isinstance(status, int) or isinstance(status, bool):
        status = None
    return error, status


def _dorossi_cc_auth_failure(result_ev, *, retry_error: str | None = None,
                             retry_status: int | None = None) -> str | None:
    """Was this rc != 0 round "the CLI has no usable sign-in"? If so return an
    evidence label, otherwise None. Pure function, never raises.

    In order:

    1. **A successfully completed result never is** (`_claude_result_succeeded`).
       An earlier api_retry said 401, a later retry succeeded and the answer came
       out, and the process exited non-zero for some other reason -- that is not
       not-logged-in.
    2. The result's `api_error_status` is 401 -> yes. **403 does not count**: not
       measured, and it is a permission_error, which may just mean this plan cannot
       use some model (switching with `/model` fixes it) or a proxy in between is
       blocking it, where "sign in on the host" would be the wrong prescription;
       the conservative direction of the criterion is "retryable" (see
       `dorossi_error_is_fatal`).
    3. The last api_retry's class is a credential class -> yes, but the result's
       status code must be empty or 403: the result is what happened **last**, and
       if it states another status code (400, 404 …) that wins, and an earlier
       retry event cannot override it. 403 is let through here because by then the
       CLI's own classification (which reads the error body, not just the status
       code) has already said it is a credential problem.
    4. Only when none of the above gives structured evidence (status code empty)
       is the text looked at: `is_error` true, the text no longer than
       `_DOROSSI_AUTH_NOTICE_MAX_CHARS`, and containing one of
       `_DOROSSI_AUTH_TEXT_MARKERS`.

    The returned label consists only of fixed vocabulary (the status code number, a
    class name from the allowlist, "result text"), with none of the CLI's raw text
    -- it goes to stderr, and stderr has the `/log tail` exit.
    """
    try:
        if _claude_result_succeeded(result_ev):
            return None
        ev = result_ev if isinstance(result_ev, dict) else {}
        raw_status = ev.get("api_error_status")
        try:
            status = (int(raw_status) if raw_status not in (None, "")
                      and not isinstance(raw_status, bool) else None)
        except (TypeError, ValueError):
            status = None
        if status in _DOROSSI_AUTH_STATUSES:
            return f"api_error_status={status}"
        if retry_error in _DOROSSI_AUTH_RETRY_ERRORS and status in (None, 403):
            suffix = f"/{retry_status}" if isinstance(retry_status, int) else ""
            return f"api_retry={retry_error}{suffix}"
        if status is not None:
            return None
        text = ev.get("result")
        if ev.get("is_error") and isinstance(text, str):
            clean = text.strip()
            if (clean and len(clean) <= _DOROSSI_AUTH_NOTICE_MAX_CHARS
                    and any(m in clean.lower() for m in _DOROSSI_AUTH_TEXT_MARKERS)):
                return "result text"
    except Exception:  # pylint: disable=broad-except
        return None
    return None


# Before being printed, an `apiKeySource` label must look like a label (measured /
# documented values: "none", "ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed
# key"). With the wrong shape nothing is printed at all -- so even if that field
# someday starts carrying a value, it cannot leak. Shared with
# `verify_dorossi_cli`.
_DOROSSI_API_KEY_SOURCE_LABEL_RE = re.compile(r"[A-Za-z0-9_./ -]{1,40}")
_DOROSSI_BARE_MODE_WARNING = (
    "[dorossi] claude -p started without instruction-file discovery (its init event has "
    "no memory_paths). That looks like bare mode - CLAUDE_CODE_SIMPLE set somewhere, or a "
    "CLI whose -p now defaults to --bare: the subscription login and the instruction "
    "files are skipped, so calls either fail as not logged in or are billed to an API key.")


def _dorossi_cc_startup_warnings(state: "_ClaudeStreamState") -> list:
    """If this invocation's init event reveals that "the CLI did not start on this
    backend's assumptions", return the sentences to print. Pure function (the caller
    hands the printing to `_warn_once`, so each sentence appears only once per
    process).

    Two things, both judged only when **an init was seen** (no init = cannot tell,
    not an alarm):

    * init has no `memory_paths` -> looks like bare mode. Measured: normal mode has
      this key under both chat-only and full tool modes (2026-09-19, local 2.1.276,
      with the bot's own argv), so it does not cry wolf in normal operation.
    * `apiKeySource` is not "none" -> the CLI is billing through an API key instead.
      This process no longer passes API-key variables to the child
      (`_DOROSSI_CC_DROPPED_ENV`), so if it still appears, the source is in the
      CLI's own settings (the settings file's env block, apiKeyHelper). Measured:
      signing in with the subscription credential variable
      `CLAUDE_CODE_OAUTH_TOKEN` still gives "none", so it does not cry wolf.
    """
    messages = []
    if getattr(state, "init_memory_paths", None) is False:
        messages.append(_DOROSSI_BARE_MODE_WARNING)
    source = getattr(state, "api_key_source", None)
    if isinstance(source, str) and source != "none":
        shown = (source if _DOROSSI_API_KEY_SOURCE_LABEL_RE.fullmatch(source)
                 else "(an unrecognised source label)")
        messages.append(
            f"[dorossi] claude -p is authenticating with {shown} instead of the "
            "subscription login (apiKeySource in its init event): billed per token, with "
            "no plan usage limit. This process does not pass API-key variables to it, so "
            "check the CLI's own settings (an env block or apiKeyHelper).")
    return messages


def _claude_result_succeeded(result_ev) -> bool:
    """Whether this `result` event is a **successfully completed** round
    (`subtype == "success"` and `is_error` not true). A wrong shape is always taken
    as not successful -- the failure direction is back to the old behaviour
    (raising)."""
    return (isinstance(result_ev, dict)
            and result_ev.get("subtype") == "success"
            and not result_ev.get("is_error"))


def _claude_stream_verdict(state: "_ClaudeStreamState", rc: int, err: str,
                           session_id: str | None, *,
                           silence_limit: float | None = None,
                           idle_limit: float = 0.0,
                           hard_limit: float = 0.0) -> str:
    """Judge the outcome of a `claude -p` stream that has **already ended**: return
    a string or raise.

    Returns `"ok"` (answer as normal) or `"budget"` (the per-invocation budget gate
    fired; wrap up gracefully, no retry, no exception) -- the caller takes the same
    return path for both. Everything else raises a typed exception.

    **The verdict order is the rule itself; do not reorder it** (the 2026-09-05
    incident: three named branches were placed after the generic handling, and the
    whole section became dead code with no signal at all):

      output silence → hard limit → idle → budget gate → usage limit →
      transient failure → no usable sign-in →
      CLI rejects a flag → cannot reach the server → resume retry.

    The last one (throw the session away and reopen) comes last because it does
    nothing at all for "quota used up", "server overloaded", "not logged in" or
    "the CLI does not recognise a flag we pass"; it only burns another call and
    throws away a conversation that could have been resumed. "No usable sign-in"
    comes after the usage limit and transient failure, so 429 / 402 / 5xx still
    take their own waits (inputs where both hold are pinned by tests). "CLI rejects
    a flag" holds only when the stream has not a single `result`, so no input can
    satisfy it together with the budget gate, usage limit or transient failure;
    together with "no usable sign-in" only via the api_retry evidence, and an
    api_retry in the stream means the CLI has already started calling the backend
    and evidently accepted the flags, so the sign-in check comes before it.

    **The three watchdog kills share one exit**: when killed, but a successful
    `result` has **already** arrived in the stream, the round was in fact already
    answered and the CLI was merely lingering afterwards (e.g. waiting for a
    background task to trigger). Then no exception is raised; it takes the rc==0
    wrap-up (including the usage-limit intercept) and does **not** fall into the
    rc != 0 classification below -- a killed process's rc is always non-zero, and
    falling through would be misjudged as a failure or trigger a resume retry. The
    2026-09-19 incident: the answer had come out 300 seconds earlier, yet the user
    saw "temporarily unable to respond".
    This exit was at first **for idle only**; the same day the owner reported
    "tasks keep getting killed", and after the limits were lengthened (the full
    hard limit is 3 hours, and background tasks push both idle and self-loop
    silence all the way to the hard limit), a background task that never triggers
    could hold a round for the full 3 hours and then **throw away the answer that
    came out long before** -- so the hard limit and self-loop silence keep the
    answer too. The boundary is unchanged: **the moment of the kill does not change
    at all** (the exit only changes "how it is judged after the kill"), and without
    a successful result all three still raise. After self-loop silence keeps the
    answer, the loop treats it as a completed round as usual rather than a silence
    retry -- a retry would rerun the already-finished round in full.

    **EOF is not success.** A killed backend, a broken pipe and a landed abort are
    all EOF; the rc tells them apart. When rc != 0 and the stream has not a single
    `result` event, `failure_reason` falls back to the stdout tail / stderr, so the
    diagnostic never becomes an empty string.

    `session_id` is the one **passed in to this invocation** (not `state.sid`):
    whether the resume retry should fire depends on whether we are resuming, not on
    which id the backend reported last.
    """
    # The watchdog exit (see the docstring): shared by the three kills. When the
    # answer is already in hand and the CLI was merely lingering after answering,
    # **no** exception is raised and the rc != 0 classification below is
    # **skipped** (a killed process's rc is always non-zero, and falling through
    # would be treated as a failure or trigger a resume retry); it takes the rc==0
    # wrap-up directly -- the usage-limit intercept still runs.
    answered = _claude_result_succeeded(state.last_result_ev)
    if state.kill_reason == "silence":
        limit = silence_limit or 0.0
        if not answered:
            # The background-task count must be printed: non-zero means this round's
            # silence had been held off by background tasks, and what killed it was
            # that suppression's wall-clock cap (`hard_limit`), not ordinary silence
            # -- the two must be distinguishable.
            print(f"[dorossi] claude -p loop-silence-killed: no output for "
                  f"{limit:.0f}s (pending tools={len(state.pending_tools)}, background "
                  f"tasks={len(state.background_tasks)}, round ceiling while "
                  f"background tasks run={hard_limit:.0f}s); "
                  f"stderr tail: {err[-300:]!r}", file=sys.stderr)
            raise _DorossiLoopSilence(
                f"claude -p produced no output for {limit:.0f}s")
        print(f"[dorossi] claude -p lingered after its final result; loop-silence-"
              f"killed after {limit:.0f}s (background tasks="
              f"{len(state.background_tasks)}), answer kept", file=sys.stderr)
    elif state.kill_reason == "hard":
        if not answered:
            # Report the SAME limit used for this run's deadline (mode-aware,
            # config-driven), not a hardcoded number.
            print(f"[dorossi] claude -p hard-killed: exceeded {hard_limit:.0f}s "
                  f"wall-clock ceiling (pending tools={len(state.pending_tools)}, "
                  f"background tasks={len(state.background_tasks)}); stderr tail: "
                  f"{err[-300:]!r}", file=sys.stderr)
            raise TimeoutError(
                f"claude -p exceeded {hard_limit:.0f}s hard wall-clock limit")
        print(f"[dorossi] claude -p lingered after its final result; hard-killed "
              f"at the {hard_limit:.0f}s wall-clock ceiling (pending tools="
              f"{len(state.pending_tools)}, background tasks="
              f"{len(state.background_tasks)}), answer kept", file=sys.stderr)
    elif state.kill_reason == "idle":
        if not answered:
            print(f"[dorossi] claude -p idle-killed: no output for {idle_limit:.0f}s "
                  f"and no tool running; stderr tail: {err[-300:]!r}", file=sys.stderr)
            raise TimeoutError(
                f"claude -p idle for {idle_limit:.0f}s (no output, no shell running)")
        print(f"[dorossi] claude -p lingered after its final result; idle-killed "
              f"after {idle_limit:.0f}s, answer kept", file=sys.stderr)
    elif rc != 0:
        reason = state.failure_reason(err)
        print(f"[dorossi] claude -p exited {rc}: {reason}", file=sys.stderr)
        # The per-round budget gate (--max-budget-usd) fired: this is neither a stale
        # session nor the plan's usage limit, but our own "per-invocation spend cap".
        # It **must be intercepted before the resume retry / usage-limit verdict**:
        # always wrap up gracefully -- no retry (that would burn the budget again),
        # no exception; let the caller return the current (usually empty) answer +
        # session id, and the self-loop treats this round as "no progress (idle)",
        # absorbed by consecutive_idle, resuming next round. The budget amount /
        # flag never goes to Discord; stderr only.
        if _dorossi_cc_budget_exceeded(state.last_result_ev):
            print("[dorossi] claude -p hit per-invocation budget cap "
                  "(--max-budget-usd); treating round as idle, no retry.",
                  file=sys.stderr)
            return "budget"
        # A plan / quota usage limit is not a "stale session" problem, so it must be
        # intercepted before the resume retry fires (which would burn another call).
        # A usage limit reports its own dedicated exception.
        usage = _dorossi_cc_usage_limit(
            state.last_result_ev, state.answer, state.sid,
            stream_reset=state.rate_reset, stream_status=state.rate_status)
        if usage is not None:
            raise usage
        # A server-side transient failure (529 Overloaded, 5xx). **It must come before
        # the resume retry**: that retry throws the session away and opens a new
        # one, which does nothing at all for "server overloaded", only burning
        # another call and throwing away a conversation that could have been
        # resumed. Instead a dedicated exception is raised, so the self-loop backs
        # off and reruns **with the same session**.
        transient = _dorossi_cc_transient_error(
            state.last_result_ev, state.answer, state.sid)
        if transient is not None:
            raise transient
        # The CLI has no usable sign-in (not logged in, credentials expired, forced
        # into bare mode). **It must come before the resume retry**: a new session
        # would fail in exactly the same way and throw away a conversation that
        # could have been resumed; and it must come **after** the usage limit and
        # transient failure, so 429 / 402 / 5xx still take their own waiting paths.
        # Outwardly still generic (the fatal branch of `_dorossi_error_hint`); this
        # stderr line must give the right prescription.
        auth = _dorossi_cc_auth_failure(
            state.last_result_ev, retry_error=state.retry_error,
            retry_status=state.retry_status)
        if auth is not None:
            bare = state.init_memory_paths is False
            hint = (" The CLI also started without instruction-file discovery (no "
                    "memory_paths in its init event): this looks like bare mode - "
                    "CLAUDE_CODE_SIMPLE set somewhere, or a CLI whose -p now defaults to "
                    "--bare - in which the subscription login is never read." if bare else "")
            print(f"[dorossi] claude -p has no usable sign-in ({auth}). Retrying cannot "
                  f"help: sign in on the host (run the CLI interactively and use /login) "
                  f"and make sure no API-key variable or setting overrides that login. Not "
                  f"retrying with a fresh session (it would fail the same way).{hint}",
                  file=sys.stderr)
            raise _DorossiAuthError(auth, bare_suspect=bare)
        # The CLI rejected a flag we passed before starting (most likely the CLI is
        # older than this code). **It must come before the resume retry**: a new
        # session would fail in exactly the same way. This stderr line must state
        # the right cause -- the `exited 1: error: unknown option …` line above reads
        # like an ordinary backend error and does not show that the thing to do is
        # update the CLI. Outwardly still generic (assembled by the bot's
        # `_dorossi_error_hint`).
        rejected = _dorossi_cc_rejected_option(state, err)
        if rejected is not None:
            print(f"[dorossi] claude -p refused to start: the installed CLI does not "
                  f"know the option {rejected!r} that this code passes. It is probably "
                  f"older than this code expects - update it. Not retrying with a fresh "
                  f"session (it would fail the same way).", file=sys.stderr)
            raise _DorossiCliOptionError(rejected)
        # Cannot reach the server (DNS, connection refused / reset). **It must come
        # before the resume retry**: throwing the session away and opening a new one
        # does nothing at all for a network outage (the new one cannot connect
        # either), and only throws away a conversation that could have been resumed
        # -- exactly how the 2026-09-22 outage made all six self-loop tasks lose
        # their context and then stop. It comes after the other named branches: the
        # usage limit / transient failure / not-logged-in each have their own more
        # precise handling, and when they hold they must not be read as an outage.
        # The caller waits for the network to come back and reruns with **the same**
        # session.
        offline = _dorossi_cc_offline(state.last_result_ev, err, session_id)
        if offline is not None:
            print("[dorossi] claude -p cannot reach its server (network); the caller "
                  "will wait for connectivity and resume the same session",
                  file=sys.stderr)
            raise offline
        # A failed `--resume` run (session_id set) always triggers a one-off retry
        # with a new session -- no longer depending on whether stderr contains the
        # 'resume'/'session' keywords, because the real failure signal appears on
        # stdout rather than stderr (an oversized / stale session cannot resume).
        if session_id:
            raise _DorossiResumeError(reason)
        raise RuntimeError(f"claude -p exited {rc}: {reason}")
    # rc == 0 (or the watchdog exit above): in rare cases a usage limit comes back
    # with rc==0, and the result text itself is the limit notice rather than a real
    # answer -- it must be intercepted here too, or the limit notice would be sent
    # out as a normal reply. **But a successful answer is not itself a notice**: a
    # long answer that happened to discuss rate limits was once judged a usage limit
    # here and thrown away whole (2026-09-17, 09-19). Text evidence must pass
    # `_dorossi_cc_limit_text_counts` (the stream says allowed -> veto; otherwise it
    # must be short enough to be a notice).
    usage = _dorossi_cc_usage_limit(
        state.last_result_ev, state.answer, state.sid,
        stream_reset=state.rate_reset, stream_status=state.rate_status)
    if usage is not None:
        raise usage
    return "ok"


def _dorossi_cc_argv(exe: str, *, session_id: str | None = None,
                     model: str | None = None, effort: str | None = None,
                     max_budget_usd: float | None = None,
                     loop_system_guidance: str | None = None,
                     extra_dir: str | None = None,
                     tools_mode: str | None = None) -> list:
    """The argv for `claude -p` (everything after `exe`). Pure function: touches no
    files, starts no process, reads no environment.

    Extracted from `_dorossi_via_claude_code` (2026-09-19, behaviour unchanged --
    the argv built before and after extraction was byte-for-byte identical across
    480 argument combinations), for one reason only: the manual verification entry
    point `verify_dorossi_cli.py` must hit the real CLI with **the same** argv. If it
    kept its own copy, it would be verifying that copy, and would never learn when
    a flag is added here.

    When `tools_mode` is omitted (None), the module global `DOROSSI_CC_TOOLS` is
    read **at call time**, as before the extraction (tests switch modes by swapping
    that global). The criterion is still "unlocks only when exactly `"full"`";
    anything else takes the two-layer chat-only lockdown -- the fail-closed
    direction does not change because of this extra parameter. For the other
    parameters' semantics, see the docstring of `_dorossi_via_claude_code`.
    """
    if tools_mode is None:
        tools_mode = DOROSSI_CC_TOOLS
    args = [
        exe, "-p",
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        # The per-round `/model` override (already mapped to a backend alias and
        # validated by the parser); unspecified keeps the default.
        "--model", (model or DOROSSI_CC_MODEL),
        # Load ZERO MCP servers (none passed via --mcp-config). The host's
        # global MCP config can cold-start health-checks that hang `claude -p`
        # for minutes inside the bot.
        "--strict-mcp-config",
        # Move the system prompt's "per-machine dynamic sections" (cwd / env / git
        # status) into the first user message, so the system prompt prefix stays
        # stable across cross-process resumes -> the prompt-cache prefix hits more
        # often (those dynamic sections can change on every call and break the
        # cache prefix). Measured: combined with --append-system-prompt below, the
        # CLI accepts it and exits 0 (the help says "with --system-prompt", but this
        # code uses --append-system-prompt and the default system prompt is still in
        # use, so the flag applies). Always passed.
        "--exclude-dynamic-system-prompt-sections",
    ]
    if effort:
        # The per-round `/effort` override of thinking effort; unspecified passes no
        # flag at all (keeping the CLI default).
        args += ["--effort", effort]
    if max_budget_usd is not None and max_budget_usd > 0:
        # A dollar spending cap per invocation (only effective on the --print path,
        # which this is). When exceeded, `claude -p` reports a non-zero exit + a
        # result event with subtype=="error_max_budget_usd", wrapped up gracefully by
        # the rc!=0 block of `_claude_stream_verdict` (no retry, no exception).
        args += ["--max-budget-usd", str(max_budget_usd)]
    if tools_mode == "full":
        # Full agent: every tool enabled and the approval gate removed (owner
        # authorised).
        args += ["--permission-mode", "bypassPermissions"]
    else:
        # Chat only (the default): Dorossi can only converse and cannot run a shell
        # or read / write files. **Two layers; their order does not matter, and
        # without either one this is not what is described here:**
        #   1. `--tools ""` -- a tool **allowlist**, and an empty one. This layer is
        #      fail-closed: tools the CLI adds later do not appear in chat-only by
        #      themselves. Measured on CLI 2.1.276 on 2026-09-19: the init event's
        #      tool list is 0 entries and chat answers as usual; a session created in
        #      full mode with tool_use in its history also answers as usual when
        #      resumed with it (switching modes needs no session reset).
        #   2. `--disallowedTools …` -- the old **enumerated denylist**, kept as the
        #      second layer. On its own it is fail-open: measured the same day, with
        #      only it, init still listed 18-22 tools (varying with the cwd),
        #      including ListAgents (lists the host's other interactive sessions)
        #      and SendMessage, which ran without approval. `--restricted` is not
        #      enough either (14 remain).
        # The empty string must be **a real empty element** in the argv: this goes
        # through exec, not a shell, and on Windows `list2cmdline` writes it as `""`,
        # which the CLI reads back as an empty tool list (confirmed the same day with
        # one real run of the production code, see that part of
        # `test_dorossi_stream`; check 1 of `verify_dorossi_cli.py` measures init's
        # tool list again every time). An older CLI that does not know `--tools`
        # refuses with "unknown option" before starting --
        # `_dorossi_cc_rejected_option` spells that out.
        args += [
            "--tools", "",
            "--disallowedTools",
            "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit",
        ]
    # The system prompt channel (stable, cacheable, not accumulated into the
    # conversation history). Measured: --append-system-prompt combined with
    # --resume "takes effect for that round and is not baked into the session", so:
    #   * A new session (no session_id): add the base system prompt (including the
    #     secrecy rules) -- appended (not replacing), keeping Claude Code's agent /
    #     tool scaffolding. Resume keeps the original, so it is not added again.
    #   * loop_system_guidance (the self-loop's durable rules, passed only by the
    #     self-loop): attached on every round (including resume / compaction
    #     rounds), making sure the rules really reach the backend every round
    #     without accumulating into the user-prompt history as the old version did.
    # The two are joined into a single --append-system-prompt passed once.
    append_parts: list[str] = []
    if not session_id:
        append_parts.append(DOROSSI_SYSTEM_PROMPT)
    if loop_system_guidance:
        append_parts.append(loop_system_guidance)
    if session_id:
        args += ["--resume", session_id]  # resume keeps the original system prompt
    if append_parts:
        args += ["--append-system-prompt", "\n\n".join(append_parts)]
    if extra_dir:
        # This conversation's extra accessible directory (specified when the
        # conversation was opened, and passed again on every later round). A
        # per-invocation flag that --resume does not remember.
        args += ["--add-dir", extra_dir]
    return args


async def _dorossi_via_claude_code(
        prompt: str, session_id: str | None,
        on_text=None, extra_dir: str | None = None,
        workdir: str | None = None, silence_limit: float | None = None,
        on_proc=None, abort_check=None,
        max_budget_usd: float | None = None,
        loop_system_guidance: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        usage_baseline: dict | None = None) -> tuple:
    """Answer via one-shot headless Claude Code (`claude -p`), so usage rides
    the host login's plan. Continuity uses Claude Code's own session: pass the
    stored `session_id` to `--resume`; the returned id is stored for next time.
    Prompt on STDIN (no shell / no argv flag-parsing of user text), neutral
    temp cwd, MCP isolated.

    Tool exposure is config-gated by DOROSSI_CC_TOOLS (bot_config.json →
    dorossi_cc_tools): "off" (default) is pure chat — an EMPTY tool allowlist
    (`--tools ""`, fail-closed) plus the enumerated `--disallowedTools` denylist
    as a second layer, no bypassPermissions, so Dorossi cannot run shell or
    touch the host filesystem; "full" is the full agent — all tools enabled and
    the approval gate removed (--permission-mode bypassPermissions), owner-
    authorized. A session created in one mode resumes fine under the other.

    Streams NDJSON events (`--output-format stream-json`) and runs a two-tier
    watchdog: (1) IDLE — interrupt if there is no output for
    DOROSSI_CC_IDLE_LIMIT_SEC AND no tool/shell is currently executing, so a
    long-but-progressing answer (or a tool that keeps emitting output) keeps
    running; (2) HARD — always kill after the mode-aware, config-driven hard
    wall-clock ceiling (_dorossi_cc_hard_limit_sec(): off ~900s / full ~10800s
    by default, overridable in bot_config.json) regardless of pending tools.
    The idle limit is `dorossi_cc_idle_limit_sec` (default 600s); pending tools
    AND CLI-reported background tasks both hold it off. The hard
    ceiling is mandatory: in "full" mode (`--permission-mode bypassPermissions`)
    a tool can block forever, which would suppress the idle tier indefinitely
    and hang the handler with no reply; it stays in force in pure-chat mode too.
    Both kills raise TimeoutError so the caller replies generically — UNLESS a
    successful `result` was already received (the CLI answered, then lingered),
    in which case the answer is kept (see `_claude_stream_verdict`; the same
    exit applies to the loop-mode silence kill).

    `on_text`, if given, is a sync callback invoked with the accumulated ANSWER
    text (the concatenation of streamed `text_delta` deltas of the assistant's
    text content blocks) as it grows — used to drive a throttled single-message
    live preview. It carries ONLY the answer text (same sanitized content as the
    final reply), never tool_use input, tool results, or raw stdout. The
    authoritative answer is still the `result` event's `result` field.

    `extra_dir`, if given, is appended as `--add-dir <extra_dir>` to grant the
    backend an additional accessible directory for THIS invocation. It is a
    per-invocation flag — it is NOT baked into the resumed session, so the
    caller must re-pass it on every turn for the whole conversation. In off
    (pure-chat) mode it is inert (tools are disallowed); no special-casing.

    `workdir`, if given, becomes the subprocess `cwd` for THIS invocation,
    replacing the default DOROSSI_CC_WORKDIR — the backend really executes there
    (loading that directory's own config). Like `extra_dir` it is a
    per-invocation choice (the resumed session does NOT remember it; in fact
    Claude Code keys its session store by a cwd-hash, so the caller MUST pass the
    SAME workdir on every turn of a conversation or `--resume` would look in a
    different store and fail). It is validated as an existing directory when it
    is WRITTEN, but read back verbatim, so it may have vanished since:
    `_dorossi_require_workdir` re-checks it right before the spawn (after the
    managed-subtree mkdir) and raises `_DorossiWorkdirError`, instead of letting
    the child die with a `NotADirectoryError` that is misreported as "CLI
    unavailable". Auto-mkdir applies ONLY inside the managed DOROSSI_CC_WORKDIR
    subtree; a user-supplied workdir is never created, and is passed UNCHANGED.

    `silence_limit`, if given, switches the watchdog to AUTONOMOUS-LOOP mode: the
    normal two-tier watchdog (idle + mode-aware hard wall-clock ceiling) is
    REPLACED by a single output-silence backstop — if there is no new stdout
    line for `silence_limit` seconds the process is killed REGARDLESS of pending
    tools (the deliberate difference from the idle tier, so a hung tool can't
    suppress the watchdog forever) and `_DorossiLoopSilence` is raised. There is
    no round cap and no wall-clock ceiling on a round that keeps producing
    output (the loop driver bounds total runtime via the per-round silence kill
    + `@bot abort`); `silence_limit` is still finite and clamped ≥ 60s at the
    config layer so this protection can never be disabled.
    ONE exception (2026-09-19): while the CLI reports background tasks
    (`state.background_tasks` — a background subagent / shell / monitor the
    turn is waiting on), a silence timeout does NOT kill — but only until the
    round is `_dorossi_cc_hard_limit_sec()` old; the first silence timeout after
    that kills as before. The readline wait stays `silence_limit` throughout,
    so a kill can only ever land at or after the moment the old code would have
    killed (strictly more lenient, never earlier). When `silence_limit` is None
    (the default, normal path) the existing two-tier watchdog is unchanged.

    `on_proc`, if given, is a sync callback invoked once with the live subprocess
    right after it is spawned — the loop driver uses it to record the handle so
    `@bot abort` can kill the in-flight round.

    `abort_check`, if given, is a sync predicate checked once right after spawn:
    if it already returns True (an abort landed in the brief window between the
    caller's last flag check and this spawn) the process is killed immediately so
    no full unattended round runs after the user asked to stop.

    `max_budget_usd`, if given and > 0, is passed as `--max-budget-usd` — a
    per-INVOCATION (this one `claude -p` call) dollar cap, NOT a round/turn cap,
    so it is compatible with the autonomous loop's no-round-cap ruling. When the
    cap is hit the CLI exits non-zero with a `result` event whose
    subtype == "error_max_budget_usd" (no `result` answer); this function
    handles that GRACEFULLY — it does NOT raise and does NOT trigger the stale-
    session resume retry (which would re-spend the budget). It returns the
    (empty) answer + session id so the caller treats the round as idle and the
    next round resumes the same session. Budget amounts never reach Discord.

    `model` / `effort`, if given, override this invocation's backend model /
    reasoning effort (this function trusts them: `effort` is a validated level
    word, `model` is an allowlist-validated value from DOROSSI_MODEL_CHOICES
    resolved by `_dorossi_session_tuning` — raw user input never reaches this
    argument). At the CLI level both are per-INVOCATION
    flags (NOT baked into `--resume` — like `extra_dir`/`workdir` the caller
    must re-pass them on every call of the conversation); the SESSION-level
    persistence lives caller-side in the session store (`tune_effort`/
    `tune_model` on the slot, re-read into every snapshot), per the owner
    ruling that `/effort`・`/model` apply to the whole session until changed.
    None keeps the defaults (DOROSSI_CC_MODEL; no effort flag at all).

    `loop_system_guidance`, if given (autonomous loop only), is appended to the
    system prompt on EVERY call — fresh AND resume. Verified: --append-system-
    prompt applies on a --resume turn and is NOT baked into the stored session,
    so re-passing it each round keeps the durable loop rules (verify/tooling
    guidance) reaching the backend every round WITHOUT them accumulating into the
    growing user-prompt transcript (the O(N²) drain). Single-turn calls leave it
    None. Because a fresh loop round bakes (system+guidance) at creation and
    resume rounds re-append the guidance, a loop-created session's resume rounds
    carry the guidance twice in the system prompt — harmless (cached, never
    accumulated) and the deliberate cost of guaranteeing every round (incl. a
    session first created as a single-turn chat) actually receives the rules.

    `usage_baseline`, if given, is the slot's stored `cc_usage_mark` — the raw
    totals the CLI reported at the end of the previous call on the resumed
    session. CLI 2.1.277+ reports `--resume` totals cumulatively, so this is what
    turns them back into per-call numbers (`_dorossi_cc_account_round`). Ignored
    on a fresh session (no `session_id`).

    Returns (answer, session_id, info) where `info` is the per-INVOCATION
    round-info dict (`cost_usd` / `in` / `cr` / `cc` / `out`, plus `ctx` = the
    last API call's context size when readable, `acct` = how the numbers were
    derived, and `usage_mark` = the raw totals the caller must persist into the
    SAME slot as the next call's `usage_baseline`) so the autonomous loop can
    accumulate per-round spend for budget-awareness / the periodic-compaction
    trigger. The info numbers are diagnostics only — never sent to Discord."""
    exe = _shutil.which("claude")
    if exe is None:
        raise FileNotFoundError("claude CLI not found on PATH")
    # The argv is built by `_dorossi_cc_argv` (a pure function); **do not write another
    # copy here**: the manual verification entry point `verify_dorossi_cli.py` uses
    # the same function to build the argv it sends to the real CLI, and if this one
    # diverged it would be verifying a copy (`test_verify_dorossi_cli` pins this
    # line).
    args = _dorossi_cc_argv(
        exe, session_id=session_id, model=model, effort=effort,
        max_budget_usd=max_budget_usd, loop_system_guidance=loop_system_guidance,
        extra_dir=extra_dir)
    # This backend call's working directory: the user's choice if given (checked to
    # exist when written, confirmed again before spawning), otherwise the default
    # workspace. A user directory is never force-created.
    if workdir:
        effective_cwd = workdir
    else:
        effective_cwd = str(DOROSSI_CC_WORKDIR)
    # Create the workspace subtree on demand. mkdir ANY cwd inside our managed
    # DOROSSI_CC_WORKDIR (the shared default OR a per-session isolated subdir
    # passed as `workdir`), but NEVER a user-supplied external `/new <path>` dir —
    # that must already exist (re-checked just below) and must not be auto-made.
    try:
        cwd_path = Path(effective_cwd)
        # Create the directory automatically only inside the subtree we manage;
        # the criterion is a single decision point, so do not write another
        # `.parents` comparison here.
        if _dorossi_cwd_is_managed(cwd_path):
            cwd_path.mkdir(parents=True, exist_ok=True)
    except Exception:  # pylint: disable=broad-except
        pass
    # Validated when written does not mean it still exists now (see
    # `_dorossi_require_workdir`); this must come after the mkdir.
    _dorossi_require_workdir(effective_cwd)
    # The hard wall-clock ceiling: mode-aware (tighter for off / wider for full) +
    # overridable in bot_config.json. Resolve the current mode's value through the
    # function; do not reference a hard-coded number. **Taken once before spawning**:
    # the same value is both the watchdog deadline below and what the CLI is given
    # as its own background-task wait cap (see `_dorossi_cc_child_env`), and the two
    # must be the same number. In loop_mode it only bounds how long "background tasks
    # holding off the silence watchdog" can last; a round that keeps producing output
    # still has no wall-clock ceiling.
    hard_limit = _dorossi_cc_hard_limit_sec()
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=effective_cwd,
        env=_dorossi_cc_child_env(hard_limit),
        limit=16 * 1024 * 1024,  # one `result` NDJSON line can be large
    )
    # Let the caller record this child process (the self-loop uses it so `@bot
    # abort` can kill the current round immediately).
    if on_proc is not None:
        try:
            on_proc(proc)
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
    # If an abort landed between "the caller's last flag check" and "the spawn
    # here", terminate at once rather than letting an unattended round run in full
    # after the user asked it to stop (readline below will hit EOF).
    if abort_check is not None:
        try:
            if abort_check():
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
    # Send the prompt, then close stdin so the CLI starts answering.
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
    except Exception:  # pylint: disable=broad-except
        pass
    # Drain stderr concurrently so a full stderr pipe can't deadlock the child.
    # **May only be reaped by the `finally` below, through `_dorossi_drain_stderr`**
    # -- it waits for EOF, and EOF is not in our hands (see the note on
    # `_dorossi_reap_proc`).
    err_task = asyncio.ensure_future(_read_stream_all(proc.stderr))

    # Self-loop mode (silence_limit set): the normal two-tier watchdog is disabled in
    # favour of a single "output silence" backstop. Otherwise keep the original idle
    # + hard wall-clock ceiling.
    loop_mode = silence_limit is not None
    idle = DOROSSI_CC_IDLE_LIMIT_SEC
    # `hard_limit` was taken before spawning (the same value was also handed to the
    # CLI, see above). The watchdog clock subtracts host sleep time
    # (`_DorossiWatchClock`, see `_dorossi_readline_watched`).
    clock = _DorossiWatchClock()
    deadline = clock.now() + hard_limit  # hard wall-clock ceiling (excluding sleep)
    state = _ClaudeStreamState(session_id)
    try:
        while True:
            if loop_mode:
                # Self-loop mode: each readline waits at most silence_limit; no hard
                # wall-clock ceiling and no round cap.
                wait = silence_limit
            else:
                # Bound each readline's wait to no more than the seconds left until
                # the hard limit, so even `if state.pending_tools: continue` can only
                # loop until the hard limit.
                remaining = deadline - clock.now()
                if remaining <= 0:
                    state.kill_reason = "hard"
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    break
                wait = min(idle, remaining)
            try:
                line = await _dorossi_readline_watched(proc.stdout, wait, clock)
            except asyncio.TimeoutError:
                if loop_mode:
                    # A background task is still there (a background subagent / shell /
                    # watch task): this silence is waiting on it, so do not kill --
                    # but only up to this round's wall-clock ceiling. **The wait length
                    # is unchanged** (still silence_limit), so the kill can only happen
                    # at or after the old behaviour's moment, never earlier; changing it
                    # to min(silence, remaining) would kill a round that had only just
                    # gone quiet early, on the eve of the ceiling.
                    # Foreground tools (pending_tools) deliberately do **not** count: a
                    # hung foreground tool is exactly what this backstop exists to stop.
                    if state.background_tasks and clock.now() < deadline:
                        continue
                    # The self-loop backstop: no new output at all within silence_limit
                    # -> always terminate, even with a tool still running (the key
                    # difference from the normal idle tier: a hung tool cannot hold the
                    # watchdog off forever). Treated as stuck, and the caller's silence
                    # retry takes over.
                    state.kill_reason = "silence"
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    break
                # No output in this stretch. First check whether the hard limit has
                # been reached -- if so, terminate the process whether or not tools
                # are pending.
                if clock.now() >= deadline:
                    state.kill_reason = "hard"
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    break
                # The hard limit is not reached yet: keep waiting if a tool / shell is
                # still running; only "truly idle" (no output and no tool running) is
                # cut by the idle watchdog. Background tasks the CLI reports count too
                # (a watch task left at the end of a turn makes the CLI wait silently
                # for it to trigger, the 2026-09-19 incident); either can only stretch
                # the wait up to the hard limit above.
                if state.pending_tools or state.background_tasks:
                    continue
                state.kill_reason = "idle"
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                break
            if not line:
                break  # EOF -- the process ended by itself. **Note this is not
                # success**: being killed / a broken pipe is EOF too, and the rc below
                # tells them apart (see `_claude_stream_verdict`).
            # Folding (a pure data transformation) moved into
            # `_ClaudeStreamState.feed`: this reading half is tied to the subprocess +
            # watchdog and cannot be tested; the folding half can be fed a fake
            # stream.
            state.feed(line.decode("utf-8", "replace").strip(), on_text)
    except BaseException:  # pylint: disable=broad-except
        # An unexpected exit (a `ValueError` / `ConnectionResetError` raised by
        # `readline`, a `CancelledError` coming in from outside): kill the process
        # **synchronously** first, rather than waiting for the bounded wait of
        # `_dorossi_reap_proc` below. The reason is that on the cancellation path we
        # are not guaranteed a chance to finish any await -- the waits in `finally`
        # are best-effort, while `proc.kill()` is not a coroutine and always
        # completes. The normal path (`break`) does not come through here, so the
        # process still gets the chance to leave properly on its own and is not
        # killed early.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        raise
    finally:
        # **Every exit path must come through here**, not just the normal end. Two
        # kinds actually reachable that the loop does not handle itself: (1)
        # `readline()` raising a non-timeout exception -- when a single NDJSON line
        # exceeds the 16MB buffer limit, `readuntil`'s `LimitOverrunError` is turned
        # into `ValueError` by `readline`, and a broken pipe is the
        # `ConnectionResetError` the transport sets on the reader; the loop's
        # `except asyncio.TimeoutError` catches neither. (2) A `CancelledError`
        # coming in from outside on abort / shutdown (a `BaseException`, which no
        # `except Exception` catches). (An `on_text` callback raising is **not** on
        # this list: `_ClaudeStreamState.feed` swallows it itself, as part of its
        # "never raises" promise, pinned by `test_dorossi_stream.py`.) The old
        # version had no try/finally here, and all three paths would leave a
        # still-running backend process (holding the host's shell in `full` mode) +
        # an unreaped stderr-draining task, while the caller was holding this
        # session's lock -- every later round could only queue behind a round that
        # would never end.
        #
        # Both waits are bounded, for the reasons written in `_dorossi_reap_proc`:
        # `await proc.wait()` and `await err_task` are **each** unbounded, and
        # previously only the two-tier watchdog guarded the inside of the loop,
        # while these two lines outside it were not guarded at all.
        rc = await _dorossi_reap_proc(proc)
        err = await _dorossi_drain_stderr(err_task)
    if rc is None:
        # Not even the rc can be obtained (the process is still there after the
        # kill). This is an abnormal host-level state, handed to the existing
        # "non-zero exit" classification -- the diagnostic falls back to the result
        # event / stdout tail.
        rc = _DOROSSI_UNREAPED_RC
        print("[dorossi] claude -p could not be reaped; "
              f"treating as rc={rc}", file=sys.stderr)
    # Start-up-shape alarms (looks like bare mode, billing through an API key): placed
    # **before** the verdict, because the verdict may raise -- and these two things
    # show up most often exactly when that round fails. Each sentence is printed
    # only once per process.
    for warning in _dorossi_cc_startup_warnings(state):
        _warn_once(warning)
    # The verdict (a pure function: raises or returns) is kept apart from the ledger
    # write (a side effect). The two "normal wrap-ups" -- "ok" and the budget gate's
    # graceful "budget" -- take the same return path; the budget one cannot fall
    # into the rc==0 usage check below because the verdict already returned at its
    # own step.
    _claude_stream_verdict(state, rc, err, session_id,
                           silence_limit=silence_limit,
                           idle_limit=idle, hard_limit=hard_limit)
    return state.answer, state.sid, _dorossi_round_info_and_record(
        state.last_result_ev, stderr_tail=err,
        cli_command=_dorossi_cli_command_of(prompt),
        resumed_id=session_id, sid=state.sid, cli_version=state.cli_version,
        baseline=usage_baseline)
