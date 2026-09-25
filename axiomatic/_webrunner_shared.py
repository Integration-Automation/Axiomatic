"""Shared pure-function layer (driver-agnostic) for the two webrunner variants.

`webrunner_novelai.py` (the Selenium variant) and `webrunner_je_only.py`
(the je_web_runner variant) were originally function-for-function isomorphic,
differing only in "how the DOM/driver gets called". This module consolidates
the pure functions / pure I/O of both variants that "**never touch the
driver**" (todo file read/write, prompt text processing, timing, retry, output
folder layout, event output) plus the constants they need, so both variants
`from _webrunner_shared import ...` share one copy and cannot drift apart.

Module boundary (hard rule):
- This module MUST be **driver-agnostic**: it may not import selenium /
  je_web_runner, nor create / hold any driver. Everything that needs a driver
  stays in each variant (the later C series of P6 will inject it via an
  adapter).
- It is one of the "passive shared modules" CLAUDE.md permits (same standing as
  `_queue_consume` / `_run_progress`): both variants import it, but the two
  variants still do **not** import **each other**, and do not import
  `discord_bot`.
- stdlib only.

This is the first cut of the P6 refactor (C1): pure-function extraction, zero
adapters. The Chrome lifecycle family (snapshot / orphan kill / lock cleanup)
stays in each variant on purpose because it is entangled with the verify-mode
module globals (`CHROME_PROFILE_SNAPSHOT` / `_SUPPRESS_ORPHAN_SWEEP`); a later
commit will move it together with the verify wiring.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import traceback
from collections.abc import Container
from pathlib import Path, PureWindowsPath

from _batch_config import load_batch_config  # permitted passive shared loader
import _run_progress  # permitted passive shared module (resume checkpoint)
import _queue_consume  # permitted passive shared module (dynamic-consume decisions)
import _code_fingerprint  # permitted passive shared module (startup code fingerprint)
# The rc contract is shared with the two supervisors (`_supervisor` is also a
# passive shared module, pure stdlib).
from _supervisor import (  # noqa: E402
    RC_GENERATION_BLOCKED,
    RC_SETUP_INCOMPLETE,
    RC_ZERO_PROGRESS,
    trim_log,
)
# The single source of `pair_todos` is _queue_consume (the natural home for the
# consumption vocabulary); it is re-exported here so this module and both
# variants (plus the ws.pair_todos test) all use one copy and cannot drift (P7).
from _queue_consume import pair_todos  # noqa: F401  (re-exported single source)
# A passive shared module permitted by `CLAUDE.md`. Only its `_pid_alive` is
# used -- do not write a fourth copy (`test_pid_liveness` reconciles the copy
# count), and this is deliberately the copy whose "can't tell -> return True"
# direction is what we want here, see `claim_liveness_signal`.
import _chrome_slot  # noqa: E402

# This file lives at `<repo>/axiomatic/_webrunner_shared.py`, so `.parent.parent`
# is the repo root; the same computation each variant uses for PROJECT_ROOT, with
# an equal value (and neither is ever reassigned).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# After this many in-a-row image failures `generate_loop` emits a
# `consecutive_failures` alert event (does NOT abort — the hard abort is the
# batch_config `consecutive_fail_abort`). Used only by generate_loop (C5).
CONSECUTIVE_FAIL_ALERT = 5
# While `wait_for_new_image` is waiting for an image, this is how often it turns
# back to ask "did a purchase / plan dialog just pop up on screen". When the
# quota runs out the image never comes, so this question originally had to wait
# for the entire 180-second timeout to burn down and return to
# `generate_one_image` before it was asked -- measured on the 2026-08-24 ~
# 08-27 log: 65 times blocked, each one an empty 180~182-second wait, 3 hours
# 16 minutes in total, and the user's notification was 3 minutes late too.
BLOCK_PROBE_INTERVAL_SEC = 2.0
# ...but **do not ask for this long right after pressing Generate**. The reason
# is that this probe can false-kill one situation: the dialog is already on
# screen while the site is simultaneously computing a real image. Over the same
# log's 410 successful generations: p50=5 s, p90=9 s, with a tail out to 80 s.
# Take p90 as the grace period -- an image that really is being computed almost
# always lands before then, and once it lands the loop picks it up first (the
# image check runs before the probe, and the existing "an image wins" priority
# is unchanged). The worst outcome of a false-kill is only that this one image
# is regenerated after the quota refills; no bad data is produced. Incidentally:
# an ordinary 5-second generation never triggers the probe at all, so the happy
# path spends not one extra round-trip.
BLOCK_PROBE_GRACE_SEC = 10.0
EVENTS_FILE = PROJECT_ROOT / "events.ndjson"
# The long-lived "a batch is running" signal. Three writers: the two launchers
# (the parent process), and the webrunner itself when there is no parent process
# (`claim_liveness_signal`). Cross-process file -> atomic write.
WEBRUNNER_PID_FILE = PROJECT_ROOT / "webrunner.pid"
OUTPUT_ROOT = PROJECT_ROOT / "output"
# The bot writes a DOM-introspection request to this file; the webrunner polls
# it at iteration boundaries.
DOM_REQUEST_FILE = PROJECT_ROOT / "dom_request.json"
# The single-image request file + the one-shot output root (used by
# serve_single_image_request, moved in with C4). One-shot images always go into
# output/_oneshot/<request_id>/, touching neither the resume checkpoint, nor the
# queue pop, nor numbering. **The two variants no longer each keep their own
# copy of this constant**: after pass 3, "is this process a single-image server"
# is declared by an argv flag (see the section below), the variant's main() no
# longer needs to touch this file, and only this module and the bot each hold a
# copy.
SINGLE_IMAGE_REQUEST_FILE = PROJECT_ROOT / "single_image_request.json"
WEBRUNNER_PAUSE_FILE = PROJECT_ROOT / "webrunner.pause"
# Queue / fallback files (read_queues + run_batch pop, C6). Newline-only,
# NBSP-normalised, trailing-newline-iff-nonempty contract lives in CLAUDE.md.
# `AUTH_FILE` stays per-variant (main shell reads credentials before boot).
TODO_PROMPT_FILE = PROJECT_ROOT / "todo_prompt.md"
TODO_FILE_1 = PROJECT_ROOT / "todo_character1.md"
TODO_FILE_2 = PROJECT_ROOT / "todo_character2.md"
TODO_UNDESIRED_FILE = PROJECT_ROOT / "todo_undesired.md"
PROMPT_FILE = PROJECT_ROOT / "prompt.md"               # main-prompt fallback
CHARACTER1_FALLBACK_FILE = PROJECT_ROOT / "character1.md"  # char1 fallback
CHARACTER2_FALLBACK_FILE = PROJECT_ROOT / "character2.md"  # char2 fallback
UNDESIRED_FILE = PROJECT_ROOT / "undesired.md"         # undesired fallback
SINGLE_IMAGE_OUTPUT_ROOT = OUTPUT_ROOT / "_oneshot"

# ---------- Is this run a "batch" or a "single-image server": declared, not inferred ----------
# **The 2026-06-27 production incident**, evidence in `webrunner.log` lines
# 1-125: a `/run` came in with 57 queue pairs (line 2 `todo quadruples: 57`,
# then all 57 characters listed), yet line 78 printed
# `startup single-image request detected; serving one-shot` -- a single-image
# request file happened to be sitting on disk, so `run_batch` swapped the whole
# batch run for a single-image server: it served two one-shot images (line 122's
# output filename `oneshot_20260627_082159.png` carries that date), then line
# 124 went idle and `return 0`. In the entire window **not a single batch image
# was generated**, and no `todo_done` was sent. And to the two supervisors rc=0
# means "ran clean", so nobody respawned, and all 57 pairs went unrun.
#
# The disease is that **the mode is inferred**: `SINGLE_IMAGE_REQUEST_FILE.exists()`
# answers "did someone queue a single image on disk", but it was used to answer
# a completely different question, "is this process a single-image server". The
# two questions' answers agree only when the bot happens to spawn us during idle;
# the moment a request file overlaps in time with a `/run`, they are opposite,
# and the failure direction is the worst one (silently skipping the entire
# queue, plus a fake success rc).
#
# The fix is to **declare** the mode. The gate (pass 3, 2026-09-12) is now this
# rule:
#     **The flag decides the mode; the file decides "what an already-running
#     batch serves next".**
# That is, `SINGLE_IMAGE_REQUEST_FILE` keeps its original in-band role (a batch
# polls it at pair boundaries and between images, and serves it in passing), but
# it **no longer** decides this process's identity.
#
# **Why argv and not an environment variable.** `discord_bot._spawn_webrunner`
# passes no `env=`, so the child process inherits the parent's environment
# directly. So an environment variable set for a single image and not cleared
# afterwards would turn **every** later batch into a server -- the exact same
# defect, just moved from disk to process state, and harder to trace: a request
# file can be seen and deleted, but an environment variable is invisible in the
# log and in Task Manager. argv, by contrast, has to be respelled on every
# spawn, and if it is spelled wrong it prints verbatim on the startup line.
RUN_MODE_BATCH = "batch"
RUN_MODE_SINGLE_IMAGE_SERVER = "single-image-server"
# The two variants' `main()` recognises this flag via `parse_run_mode(sys.argv)`;
# what wires it onto argv is the bot's
# `_spawn_webrunner(..., single_image_server=True)`. There is only one copy of
# the literal, which the bot imports -- copy a second one and after a rename the
# spawn still succeeds, only that process no longer knows what it is (exactly the
# shape of the incident above).
SINGLE_IMAGE_SERVER_FLAG = "--single-image-server"

# Chrome's renderer-crash interstitial title markers (URL `chrome-error://` is
# the definitive marker; titles vary by locale / crash flavour). Kept here so
# both webrunner variants share ONE list — keep in sync with any bot mirror.
_CHROME_CRASH_TITLE_MARKERS = (
    "aw, snap", "aw,snap", "he's dead", "he's just resting", "out of memory",
)

# ---------- dead browsing context (chrome cut off mid-run) ------------------------
# The fingerprint of "the window / tab / session is gone", kept separate from
# the **transient** chromedriver stall that the hot-path reader already absorbs
# with None / False. The two need opposite reactions: a stall is worth a
# continue poll (Chrome is just busy), but once the session is gone **every**
# subsequent call throws the exact same error, and polling only burns the whole
# retry budget (measured: one closed window spins until `consecutive_fail_abort`
# fires, about 30 minutes) while the browser stays dead the whole time.
# Message markers are matched as lowercase substrings of `str(error)`; class
# names are matched against the entire MRO, so selenium subclasses count too.
# Plain strings are used on purpose rather than importing selenium -- this module
# must be driver-agnostic.
_SESSION_GONE_MESSAGE_MARKERS = (
    "no such window",                        # window/tab closed mid-run
    "target window already closed",          # the exact line users reported
    "web view not found",                    # its `from unknown error:` 2nd line
    "invalid session id",                    # chromedriver dropped the session
    "session deleted because of page crash",
    "not connected to devtools",             # the devtools socket dropped
    "chrome not reachable",                  # the browser process is entirely gone
    "unable to connect to renderer",
    "tab crashed",
)
#
# **This list only has any effect when the matching class is actually caught by
# `port.TRANSPORT_ERRORS`.** Before 2026-09-09 `MaxRetryError` /
# `NewConnectionError` were the counterexample: they were written here, but both
# variants' tuple was only `(WebDriverException, ReadTimeoutError, OSError)`, and
# those two classes' MRO **does not pass through `OSError`**, so the hot path's
# `except` did not catch them -> `_note_transport_error` was never called ->
# this table never got a chance to speak. In production it looked like the three
# 09-07 `critical_error`s: the message was a raw `MaxRetryError`, with no
# `browser session gone during …` prefix.
# `test_variant_parity.test_every_session_gone_name_is_actually_catchable` now
# ties "the table" and "the catcher" together -- adding a name here means
# confirming that class can reach the tuple.
_SESSION_GONE_EXC_NAMES = frozenset({
    "NoSuchWindowException",       # selenium: window/target already closed
    "InvalidSessionIdException",   # selenium: session already deleted
    "NoSuchDriverException",       # selenium 4.x: the driver is gone
    # The following are on the urllib3 side. The criterion is **whether the
    # connection was established**: could not connect / refused / dropped
    # mid-way, which for a loopback listener means "nothing is listening on that
    # port" = chromedriver.exe has already exited. By contrast `ReadTimeoutError`
    # (connected, request sent, 120 s with no reply) means chromedriver is still
    # alive, so it is **deliberately omitted** -- it is the canonical "stall",
    # and listing it here would turn one busy moment into one respawn.
    "MaxRetryError",               # urllib3: the socket to chromedriver was refused
    "NewConnectionError",
    "ConnectTimeoutError",         # connect-phase timeout (base of NewConnectionError)
    "ProtocolError",               # connection dropped mid-way by the peer (RemoteDisconnected)
    "ConnectionRefusedError",      # the chromedriver process is dead
})
# JS probe: only returning this literal counts as "one full driver round-trip
# succeeded". Using the **return value** (rather than "no exception thrown") as
# the liveness criterion is deliberate -- the je variant's wrapper swallows every
# driver exception and always returns None, and on that path "no exception
# thrown" says nothing at all about whether the browser is still alive.
_ALIVE_PROBE_TOKEN = "je-alive"
_ALIVE_PROBE_JS = f"return '{_ALIVE_PROBE_TOKEN}';"

# Shared visibility criteria. **Do not use `offsetParent === null` to judge
# dialogs/toasts any more.**
#
# `offsetParent` **always returns null** for `position: fixed` elements (per the
# spec), and modal overlays and toasts are almost always fixed -- so "the dialog
# in the dead centre of the screen blocking everything" reads as "does not
# exist" in the scan. The failure mode is **silent**: detection returns None, no
# close is clicked, the diagnostic list is blank, all three layers blind at once,
# and the log shows only "generation timeout".
#
# Empirical proof (2026-08-29, real DOM in `verify_quota_dialog.py`): adding
# `position:fixed` to `role="dialog"` made Tier 1 / Tier 2 / dismiss / the
# control list **all** come back empty, while the same DOM was entirely normal
# once fixed was removed.
#
# Switched to `getClientRects()`: `display:none` (including ancestors) returns an
# empty array, and a visible fixed element returns rectangles. Add one computed
# style check to also block `visibility: hidden`, which the old criterion missed
# -- for that one `offsetParent` is **non**-null, i.e. it used to be misjudged as
# "there is a dialog".
#
# Applied only to the dialog/toast sections. Ordinary page buttons and inputs
# keep using `offsetParent`: those elements will not be fixed, and `offsetParent`
# is much cheaper (about fifty other uses in this file).
#
# **Two criteria, used at different layers, because the two misjudgements cost
# the opposite:**
# - `onScreen` (containers: dialogs, toasts) additionally requires "the rectangle
#   intersects the viewport". Misjudging "a dialog is blocking" costs an hour of
#   empty waiting, so this layer would rather be strict. The site's pages often
#   keep opacity:0 or off-screen fixed modal shells, which `getClientRects()`
#   alone would all count as visible.
# - `visible` (controls **inside** the dialog) only requires "has layout and is
#   not hidden". Missing a real Cancel is the bigger harm here -- a button
#   scrolled off-screen in a long dialog is still clickable.
_JS_VISIBLE = r"""
function visible(el) {
  if (!el) return false;
  if (el.getClientRects().length === 0) return false;   // display:none / not laid out
  const st = getComputedStyle(el);
  if (st.visibility === 'hidden' || st.display === 'none') return false;
  return parseFloat(st.opacity) !== 0;
}
function onScreen(el) {
  if (!visible(el)) return false;
  const vw = window.innerWidth || 0, vh = window.innerHeight || 0;
  for (const r of el.getClientRects()) {
    if (r.width > 0 && r.height > 0
        && r.right > 0 && r.bottom > 0 && r.left < vw && r.top < vh) return true;
  }
  return false;
}
"""

# ---------- The site blocks generation (purchase / plan information) ------------------------
# When the quota runs out the site pops up purchase / subscription information,
# and generation will never succeed again. This is **not** a retryable failure:
# the supervisor respawning the browser only sees the same dialog, so it becomes
# an infinite respawn (each round also reruns login + setup). Once detected, it
# must take the "stop cleanly, notify the user, do not respawn" path.
#
# Two detection layers, deliberately separate:
#   Tier 1 (`_GENERATION_BLOCK_JS`): dialog/toast text matches purchase semantics
#     -> stop immediately.
#   Tier 2 (`has_blocking_dialog`): **depends on no wording** -- when consecutive
#     failures reach the abort threshold, if a visible modal dialog is still
#     blocking the screen, treat it as "needs a human" rather than a crash. This
#     layer is the insurance for "the site rewrites the wording / changes
#     language / switches to a block we have not read".
#
# The wording list is a **starting guess**, not the truth: every stop writes the
# dialog's full text to the log (stderr, not leaked), and the patterns here are
# refined from that actual text. When adding a pattern always use a **phrase**,
# never a single word -- "subscription" and "purchase" appear alone in nav bars /
# footers all the time, and single-word matching would misjudge a normal page as
# blocked.
#
# ---- Wording baseline (measured over the whole `WEBRunner.log` on 2026-09-09) ------------
# **Only one entry in this list is written from actual text; the other ten are
# still guesses.** The log covers 08-24 -> 09-09, 259 blocks, and
# `[blocked] dialog text:` had **only one distinct content**:
#
#   "The paint's run dry. You need a subscription or to purchase Anlas to
#     continue. Compare and pick the right plan for you." (the apostrophe is U+2019)
#
# Whereas the old wording that this list's opening `/not enough anlas/i` describes
#
#   "Not enough Anlas. Purchasing more Anlas lets you keep generating at this
#     resolution."
#
# appears **0 times** in the same log. That is, it has never once matched on this
# site -- like the other nine it is a sentence shape written from imagination
# originally. **Keeping it is right** (it still catches if the site reverts to
# similar wording, and it has zero false positives), but do not treat it as
# "verified".
#
# The only entry that matched actual text is `/(purchase|buy) (more )?(anlas|credits)/i`
# **alone**, with zero margin: replace that "or to purchase Anlas" and the whole
# list drops from 1/11 to 0/11. Three reasonable rewrites measured
# ("requires a subscription" with the word order reversed / "need an active plan"
# / the currency word swapped to tokens) all had **0 matches** before these two
# were added.
#
# The cost of a miss is **moderate, not severe**: Tier 2 (`has_blocking_dialog`)
# depends on no wording, and when consecutive failures reach the abort threshold
# it catches as long as a modal is still on screen, so the worst is a few extra
# wasted retries.
#
# **The opposite direction (a false positive) costs more than a miss, so here we
# would rather be conservative.** A Tier 1 hit on the batch path is not a
# "clean stop" -- it takes `wait_for_quota_recovery`, and `quota_wait_max_sec`
# defaults to 0 = no limit, so a single false positive is a **wake-once-an-hour,
# never-ending wait loop**, and on the event stream it looks exactly like a real
# quota exhaustion (it still sends `quota_blocked` / `quota_wait`). Before adding
# a pattern, always measure false positives against "normal page text", not just
# positive hits.
#
# Two candidates **rejected** by this criterion, with the reasons noted here so
# nobody proposes them again next time:
#   - `/pick the right plan/i`: marginal contribution is zero (measured: every
#     wording that would hit it, the two new entries above already hit), and it
#     is the only candidate that bites the negative corpus -- a real plan
#     comparison page literally says this. Zero gain + false-positive risk.
#   - `/subscription or to (purchase|buy)/i`: written against this exact version's
#     wording, and gone the moment the site changes "or" to "and"; the "needs a
#     subscription" entry already covers every situation it could catch.
# Tier 1's container length cap, mirrored on the Python side (in the JS it is a
# literal -- that section is a raw string and cannot take an f-string
# interpolation). Used only to compute the "margin" from the log; a mismatch
# between the two sides is caught by
# `test_only_tier1_gates_the_dialog_text_on_length`, which extracts it directly
# from the JS source.
_TIER1_TEXT_CAP = 1200

_GENERATION_BLOCK_JS = _JS_VISIBLE + r"""
const SELECTOR = [
  '[role="dialog"]', '[role="alertdialog"]', '[aria-modal="true"]',
  '[class*="modal"]', '[class*="Modal"]',
  '[class*="dialog"]', '[class*="Dialog"]',
  '[role="alert"]', '[aria-live="assertive"]', '[aria-live="polite"]',
  '[class*="toast"]', '[class*="Toast"]'
].join(',');
const PATTERNS = [
  /not enough anlas/i,
  /insufficient (anlas|credits|funds|balance)/i,
  /out of (anlas|credits|generations)/i,
  /no (anlas|credits) (left|remaining)/i,
  /(purchase|buy) (more )?(anlas|credits)/i,
  /(subscribe|subscription) (is )?(required|needed|to continue)/i,
  // ↓ Two entries refined from the **actual text** in the log on 2026-09-09,
  // see "Wording baseline" below. This one fixes a word-order gap in the
  // previous entry: the previous one required required/needed/to continue
  // **immediately after** subscription, but the site wrote "You need a
  // subscription **or** to…", which it could not catch; and the reverse order
  // "Generating **requires a** subscription" it could not catch either. The
  // criterion is now a "need <-> subscription" semantic pairing, covering both
  // orders.
  /(need|needs|requires?|required) (a |an )?(subscription|paid plan)/i,
  // The title sentence of this version of the site's paywall. **This entry is a
  // cheap site-specific insurance, not a semantic criterion** -- it stops
  // working the moment the site changes the title, which is expected; do not
  // loosen it to a single phrase like /run dry/ to "make it more change-proof".
  // The `.{0,3}` is to accept the curly apostrophe (U+2019, which is what is in
  // the log), the straight apostrophe, and no apostrophe, all three.
  /paint.{0,3}s run dry/i,
  /(subscription|plan) (has )?(expired|ended|lapsed|inactive)/i,
  /upgrade your (plan|subscription|account)/i,
  /renew your (subscription|plan)/i,
  /free (trial|generation|generations) [^.]{0,40}(ended|expired|over|used up)/i,
  /payment (is )?(required|failed)/i
];
for (const node of document.querySelectorAll(SELECTOR)) {
  if (!onScreen(node)) continue;
  const text = (node.innerText || node.textContent || '')
    .replace(/\s+/g, ' ').trim();
  // Anything too long is a wrapper enclosing half the page, not the dialog
  // body itself. **This cap is Tier 1-specific**: the SELECTOR here is very wide
  // (toasts, `class*=modal`, `aria-live`), so it needs this to block false
  // positives, and a Tier 1 false positive = an unbounded wait loop. Tier 2's
  // selector is already narrow, so do not copy this line over -- the reason is
  // written above `_BLOCKING_DIALOG_JS`.
  // The number must match the Python side's `_TIER1_TEXT_CAP` (a guard test
  // reconciles it).
  if (!text || text.length > 1200) continue;
  for (const re of PATTERNS) {
    if (re.test(text)) {
      // `length` is the **full** text length (`text` has already been sliced to
      // 400). Its sole reason to exist is to turn "how much margin is left below
      // that 1200" into a measured number -- the site adding one more plan row
      // could cross it, and crossing it is silent (the whole thing then counts
      // as no dialog).
      return {text: text.slice(0, 400), pattern: String(re),
              length: text.length};
    }
  }
}
return null;
"""

# Dismiss the blocking dialog. **This section's first duty is "never click a
# button that spends money"** -- on a quota-exhausted dialog the "purchase"
# button is usually more prominent than "cancel" and more likely to be hit by a
# loose selector. So the rule is a whitelist, not a blacklist:
#   1. Only pick a button whose **text exactly equals** a known dismiss word
#      (`DISMISS`, exact match, not contains).
#   2. Or a button whose `aria-label` contains close/dismiss **and** contains no
#      payment semantics.
#   3. Or "the wordless icon button in the top-right corner" -- geometry +
#      semantic emptiness, see `byCorner`'s description below.
#   4. None of the above -> return 'escape', and the caller sends the Escape key,
#      handing over no element at all.
# Once picked it returns `{action, el}`, and **this section does not click
# itself** -- the click is sent by `_click_dismiss_target` via the driver (reason
# in section 2 below). So the safety property's decision point is the **return
# value**: "it will not return a control that spends money".
# `FORBIDDEN` covers more than purchase/buy -- ambiguous words like `ok` / `yes` /
# `confirm` / `continue` are also all forbidden: on a purchase dialog they mean
# "confirm the charge".
#
# **The second-layer dialog has a button that causes real damage: `Unsubscribe`.**
# This layer is more dangerous than the first: the first's worst case is spending
# money, this layer's worst case is **cancelling the subscription**, which stalls
# the entire production line.
#
# **`get started` / `gift key` / `anlas` were added to `FORBIDDEN` on 2026-09-07
# as defence in depth, not because they cannot be blocked now.** Scanning every
# button on both dialog layers against `FORBIDDEN` shows the protection is very
# unevenly thick:
#
#   Unsubscribe / Update Payment Details / Subscribe / Pay As You Go  -> hit
#   Get Started / Anlas / Activate a Gift Key                         -> **not hit**
#
# The latter three were originally blocked **solely by the "text must be empty"
# rule**, without even the "happens to contain subscribe" second layer that
# `Unsubscribe` has. And that rule is also rule 3's functional condition, so
# whoever loosens it will not realise they are touching a safety property. Only
# after adding these did the two truly separate.
#
# **Adding these three words does not affect close-button recognition**: a close
# button either has empty text (rule 3), or is a `close` / `cancel` / `×` sort of
# thing (rule 1's `DISMISS` whitelist) -- no reasonable close button is named
# `Get Started` or `Anlas`. `anlas` is the site's currency name, so any future
# button mentioning it is worth blocking. It is written as `get[- ]?started`
# because what `identityOf` reads is often the hyphenated form
# (`data-testid="get-started"`).
#
# **This section only "picks", it does not "click". The picked element is
# returned to Python, and the driver sends the real click
# (`_click_dismiss_target`).** This directly moved the decision point of this
# project's most expensive safety property: it used to be "the JS will not
# `click()` a button that spends money", now it is "the JS will not **return** a
# button that spends money". Every counterexample test must be pinned to the
# return value -- pinning to "was there a click event" verifies nothing (in the
# new version it is necessarily empty).
#
# Why switch to driver click -- settled only after a round trip; both parts are
# recorded because each is right on its own, just incomplete:
#
# 1. **The loop is needed.** After rule 3 shipped, five consecutive quota cycles
#    all printed
#    `could not dismiss blocking dialog via 'clicked-corner:button@805,21 32x32'`,
#    which looked like "found the right one but could not press it". What refuted
#    that was the **dialog size** that `describe_dialog_controls` printed -- it
#    changed at the exact moment rule 3 shipped:
#
#      before 11:44: 856x917, 11 candidates (Subscribe / Pay As You Go / Get Started)
#      after 11:44:  420x322, 5 candidates (Unsubscribe / Update Payment Details)
#
#    And that list is captured **after the dismiss action ran**. So the synthetic
#    click did close the paywall, exposing a **second-layer** dialog behind it,
#    and `has_blocking_dialog` saw a modal still there and reported failure.
#
# 2. **But the loop cannot solve the second layer; the synthetic click has no
#    effect on it.** Over the four quota cycles after the loop shipped, the log
#    shape became: layer 1 @(805,21) pressed and the screen really changed;
#    layer 2 @(369,21) pressed and **not one thing changed**, the early-stop
#    criterion gave up on the spot, and it still fell to a full-page reload.
#
#    The close button's class is identical on both layers
#    (`sc-2f2fb315-2 sc-29539429-20 sc-1336beac-0 eTBYIC jjGTfR fJg`), the same
#    component, the same visuals, yet one accepts the synthetic click and one
#    does not. This actually supports "the difference is **above the element**"
#    rather than "the component itself differs": `el.click()` **dispatches
#    directly to that element** and only bubbles upward; a real click's `e.target`
#    is "the topmost element at that coordinate". Also `el.click()` dispatches
#    only the `click` event, while a real click runs the full chain pointerdown /
#    mousedown / mouseup / click -- hanging the close behaviour on `onPointerDown`
#    / `onMouseDown` is a common pattern for modal components (to avoid
#    click-through), and that kind is likewise received only by a real click.
#
# 3. **The second layer is not a "needs the user" account anomaly** -- this was
#    checked out before acting: the return text of `has_blocking_dialog` in
#    `chromedriver.log` was
#    "You are subscribed to the Opus tier! Your subscription renews around
#    2026/09/18" -- just the account management panel hidden behind the paywall.
#    The behaviour matches too: after that it refilled and generated hourly as
#    usual (8.0 images/hour, consistent with the historical baseline of 7.85). So
#    the correct handling is "close it", not "stop and call a human".
#
# Two lessons recorded together: **a single log message ("cannot dismiss") tells
# you the result, not the mechanism**, so before switching mechanisms find an
# independent measurement to cross-check (section 1 was rescued once by dialog
# size); but **a cross-check proves only the one thing it proves** -- the size
# changing only proves the first layer was closed, it does not prove the second
# layer will be closed too.
_DISMISS_DIALOG_JS = _JS_VISIBLE + r"""
const FORBIDDEN = /(purchase|buy|subscribe|subscription|upgrade|pay|payment|checkout|order|confirm|continue|proceed|accept|agree|ok|okay|yes|renew|top ?up|add funds|get more|get[- ]?started|gift[- ]?key|anlas)/i;
const DISMISS = /^(cancel|close|not now|later|maybe later|dismiss|no thanks|no,? thanks|back|×|✕|✖|x)$/i;
// Rule 3's two thresholds. **Neither can be removed, they block different
// things**, and the easiest mistake is that one looks redundant:
//   CORNER_MAX_PX  blocks "in the corner, but large" -- the full overlay, the
//                  dialog's own header container. Their top-right coordinate is
//                  **exactly the same** as the close button, only the size
//                  separates them.
//   CORNER_FRAC    blocks "small, but not in the corner" -- the wordless icon
//                  buttons in the dialog's centre/bottom. Their size is
//                  **exactly the same** as the close button, only the position
//                  separates them.
// Empirical basis (the `describe_dialog_controls` list from 2026-09-03 ~ 09-07,
// the 856x917 purchase dialog, 11 candidates): the only two with empty text are
// the top-right pair, 32x32 @(805,21); the ones that spend money (Pay As You Go
// / Subscribe / Get Started / Anlas) **all have text**, and Anlas is only 37x24
// -- the size condition alone cannot block it, what blocks it is "semantic
// emptiness".
const CORNER_MAX_PX = 48;
const CORNER_FRAC = 0.2;
// Candidate controls. Beyond `button` it also takes `[aria-label]` / `[title]` /
// `[tabindex]` / `[onclick]`: the site's close button is often a div wrapping an
// svg, not a <button>. **Only the element shape was loosened; not one word of
// the wording whitelist was changed**, so the "never click a button that spends
// money" property is unchanged.
const SELECTOR = 'button,[role="button"],a[href="#"],[aria-label],[title],'
               + '[tabindex],[onclick]';
// All three rules take the "innermost" hit. After loosening the selector, a
// wrapper enclosing <button>Cancel</button> also matches (its innerText is
// likewise Cancel), and `el.click()`'s target is that wrapper, which is not
// passed to the child node, i.e. nothing is clicked.
function innermost(list) {
  for (const el of list) {
    if (!list.some(other => other !== el && el.contains(other))) return el;
  }
  return list[0] || null;
}
function labelOf(el) {
  return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
}
function attrOf(el, name) {
  return (el.getAttribute(name) || '').replace(/\s+/g, ' ').trim();
}
// The text the user **can read**. Empty = this is a pure icon button, which is
// rule 3's safety property itself. It collects more widely than labelOf, because
// "innerText is empty" does not mean "no text":
//   <input type="button" value="Purchase">  innerText empty, value has text
//   <button><img alt="Buy"></button>        innerText empty, child alt has text
function readableOf(el) {
  const parts = [labelOf(el), attrOf(el, 'aria-label'), attrOf(el, 'title'),
                 attrOf(el, 'value'), attrOf(el, 'alt')];
  for (const kid of el.querySelectorAll('[alt],[aria-label],[title]')) {
    parts.push(attrOf(kid, 'alt'), attrOf(kid, 'aria-label'),
               attrOf(kid, 'title'));
  }
  return parts.filter(Boolean).join(' ').trim();
}
// The developer-chosen identifier, used by FORBIDDEN as defence in depth.
// **The obfuscated class name is deliberately not collected**: the site's class
// is a random hash like `sc-2f2fb315-2 eTBYIC jjGTfR`, and the odds of it
// colliding with a two-or-three-letter word like `ok` / `pay` are not low, and
// the cost of a false positive is rule 3 silently failing entirely.
function identityOf(el) {
  return [attrOf(el, 'id'), attrOf(el, 'name'), attrOf(el, 'data-testid'),
          attrOf(el, 'data-test'), attrOf(el, 'data-action')]
         .filter(Boolean).join(' ').trim();
}
for (const node of document.querySelectorAll(
       '[role="dialog"],[role="alertdialog"],[aria-modal="true"]')) {
  if (!onScreen(node)) continue;
  const controls = Array.from(node.querySelectorAll(SELECTOR))
    .filter(visible);
  const byText = controls.filter(el => {
    const label = labelOf(el);
    return DISMISS.test(label) && !FORBIDDEN.test(label);
  });
  const hitText = innermost(byText);
  if (hitText) {
    return {action: 'clicked:' + labelOf(hitText), el: hitText};
  }
  const byAria = controls.filter(el => {
    const aria = el.getAttribute('aria-label') || el.getAttribute('title') || '';
    return /close|dismiss/i.test(aria) && !FORBIDDEN.test(aria)
           && !FORBIDDEN.test(labelOf(el));
  });
  const hitAria = innermost(byAria);
  if (hitAria) {
    return {action: 'clicked-aria:' + (hitAria.getAttribute('aria-label')
                                       || hitAria.getAttribute('title') || ''),
            el: hitAria};
  }
  // Rule 3: the wordless icon button in the top-right corner. The site's close
  // button is semantically completely empty (no text, no aria-label, no title,
  // not even an svg -- the X is drawn in CSS), so rules 1 / 2 all fail on it;
  // measured **218/218 times** all fell to 'escape', and Escape is likewise
  // ineffective on this modal -- meaning every quota cycle pays for one full-page
  // reload + refilling the fields.
  // (There are also 153 "dismissed" entries in the log that the old version
  //   printed unconditionally before verifying -- fake messages; do not count
  //   them as a success rate. The real criterion is "the day's reload count ==
  //   that day's attempt count".)
  const box = node.getBoundingClientRect();
  const byCorner = controls.filter(el => {
    // (a) Semantic emptiness. This is rule 3's **functional** condition: the
    // close button is a pure icon button.
    if (readableOf(el)) return false;
    // (d) Defence in depth. Scan two sources, and **deliberately overlap with
    // (a)**:
    //   - `identityOf` (id / name / data-testid) -- once (a) passes, only this
    //     could trigger, e.g. id="purchase-more".
    //   - `readableOf` -- while (a) is still in place this is **necessarily an
    //     empty string**, and it looks like dead code. What it buys is "even if
    //     someone loosens (a), it still will not click the purchase button".
    //     Reason: the site's two dialog layers have three buttons (`Get Started`
    //     / `Anlas` / `Activate a Gift Key`) **blocked solely by (a)**, and (a)
    //     is simultaneously the functional condition for "can rule 3 find the
    //     close button" -- one check doing double duty as functional and safety,
    //     and whoever changes it will not realise they are touching a safety
    //     property. After splitting them, loosening (a) only makes rule 3 fail
    //     (cannot find the close button -> fall back to reload, safe), rather
    //     than making it click the purchase button.
    //     Guard: `test_forbidden_alone_still_blocks_every_paying_button` (with
    //     (a) turned off, FORBIDDEN must still block every button on both dialog
    //     layers).
    if (FORBIDDEN.test(readableOf(el))
        || FORBIDDEN.test(identityOf(el))) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return false;      // a 0-size shell
    // (b) Small. Blocks the full overlay / header container -- their corner
    // coordinate is identical to the close button's.
    if (r.width > CORNER_MAX_PX || r.height > CORNER_MAX_PX) return false;
    // (c) In the top-right corner. Blocks wordless icon buttons elsewhere in the
    // dialog -- their size is identical to the close button's.
    if (r.right < box.right - box.width * CORNER_FRAC) return false;
    if (r.top > box.top + box.height * CORNER_FRAC) return false;
    return true;
  });
  const hitCorner = innermost(byCorner);
  if (hitCorner) {
    const r = hitCorner.getBoundingClientRect();
    return {action: 'clicked-corner:' + hitCorner.tagName.toLowerCase()
                    + '@' + Math.round(r.left - box.left) + ','
                    + Math.round(r.top - box.top)
                    + ' ' + Math.round(r.width) + 'x' + Math.round(r.height),
            el: hitCorner};
  }
  return 'escape';
}
return null;
"""

# The Escape fallback: used when there is nothing safe to click on the dialog.
# React's old event system needs `keyCode`/`which` supplied (same reason as
# `_dismiss_autocomplete`).
#
# **Dispatch to three targets, not just `document`.** An event from `document`
# only bubbles up to `window`, it **does not travel down**; a component that
# hangs keydown on the modal container or the focused element (focus-trap
# libraries very often do this) therefore never receives it. Empirical basis:
# over this project's 86 quota dialogs, Escape never once dismissed one, 100%
# fell to "cannot dismiss -> reload". This section still sends a **synthetic**
# event (`isTrusted === false`); the driver-layer real keypress goes through
# `port.press_escape()`, which `dismiss_blocking_dialog` tries first.
_SEND_ESCAPE_JS = _JS_VISIBLE + r"""
const targets = new Set([document]);
if (document.activeElement) targets.add(document.activeElement);
for (const node of document.querySelectorAll(
       '[role="dialog"],[role="alertdialog"],[aria-modal="true"]')) {
  if (onScreen(node)) targets.add(node);
}
for (const target of targets) {
  for (const type of ['keydown', 'keyup']) {
    target.dispatchEvent(new KeyboardEvent(type, {
      key: 'Escape', code: 'Escape', keyCode: 27, which: 27,
      bubbles: true, cancelable: true
    }));
  }
}
return targets.size;
"""

# When it cannot be dismissed, list **which controls** are on the dialog. Purely
# read-only, clicks not a single element.
#
# Why it is needed: when `dismiss_blocking_dialog` fails to close, it leaves only
# "cannot dismiss", and whoever reads the log has no way to know whether "the
# site simply provided no close button" or "it did, but the selector could not
# recognise it" -- and the handling for those two is completely opposite. Without
# this list the question can only be guessed at, and guessing your way into
# loosening the click rules on a **purchase dialog** is precisely the thing you
# should least do.
#
# The sort deliberately uses "distance from the dialog's top-right corner": the
# close button is almost always in that corner, so the most suspicious one sorts
# to the front and is not stranded beyond the 30-entry cap. The output is bounded
# (at most 30 entries, each field truncated), because it is written into
# WEBRunner.log.
_DIALOG_CONTROLS_DIAG_JS = _JS_VISIBLE + r"""
for (const node of document.querySelectorAll(
       '[role="dialog"],[role="alertdialog"],[aria-modal="true"]')) {
  if (!onScreen(node)) continue;
  const box = node.getBoundingClientRect();
  const rows = [];
  for (const el of node.querySelectorAll('*')) {
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const style = getComputedStyle(el);
    const clickable = style.cursor === 'pointer'
      || el.tagName === 'BUTTON' || el.tagName === 'A'
      || el.getAttribute('role') === 'button'
      || el.hasAttribute('aria-label') || el.hasAttribute('title')
      || el.hasAttribute('onclick') || el.hasAttribute('tabindex');
    if (!clickable) continue;
    rows.push({
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      aria: (el.getAttribute('aria-label') || '').slice(0, 40),
      title: (el.getAttribute('title') || '').slice(0, 40),
      text: (el.innerText || el.textContent || '')
              .replace(/\s+/g, ' ').trim().slice(0, 40),
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 60),
      icon: el.querySelector('svg,img,path') ? 1 : 0,
      dx: Math.round(r.left - box.left),
      dy: Math.round(r.top - box.top),
      w: Math.round(r.width),
      h: Math.round(r.height),
      cursor: style.cursor,
      corner: Math.round(Math.hypot(box.right - r.right, r.top - box.top))
    });
  }
  rows.sort((a, b) => a.corner - b.corner);
  return {w: Math.round(box.width), h: Math.round(box.height),
          total: rows.length, controls: rows.slice(0, 30)};
}
return null;
"""

# For Tier 2: is there a "visible modal dialog with content" on screen. Looks at
# no wording. Recognises only genuine modal semantics (`role="dialog"` /
# `aria-modal`), excluding toasts and `class*=modal` -- those are too wide, and
# normal pages often carry a hidden modal container.
#
# **There is deliberately no length cap here; do not copy Tier 1's
# `text.length > 1200` over.** The defect fixed on 2026-09-09 was exactly "both
# layers share the same cap line" -- **two guards sharing one precondition are
# not two layers.** A modal whose innerText exceeds the cap makes Tier 1 skip it
# **and** Tier 2 return None, both layers blind at once. The way to tell "is this
# the second layer" is not whether it has its own selector/pattern, but whether
# **it gets closed by the same input**.
#
# Two reasons for removing it, different directions but the same conclusion:
#
# 1. **The narrowing is already done by the selector.** Tier 1's SELECTOR is very
#    wide (including `[class*="modal"]`, toasts, `aria-live`), so it needs
#    "anything too long is a wrapper enclosing half the page" to block false
#    positives; Tier 2 recognises only `role=dialog` / `alertdialog` /
#    `aria-modal` + `onScreen()`, so only a genuine modal could pass in the first
#    place. Length does no narrowing work here, it is purely a leftover copied
#    from Tier 1.
# 2. **The cost asymmetry runs the other way, so Tier 2 should be lenient.** A
#    Tier 1 false positive is expensive -- it takes `wait_for_quota_recovery`, and
#    `quota_wait_max_sec` defaults to 0 = no limit, so one false positive is a
#    never-ending wait loop -- so Tier 1 would rather be conservative. A Tier 2
#    false positive is cheap: it is only asked at the `consecutive_fail_abort`
#    threshold (by which point the whole character is already dead) and by the
#    "dismiss-dialog loop's progress criterion", and in the latter, never
#    returning None only makes `stop_reason` land on `same`/`still` ->
#    `return False` -> the caller does a full-page reload (safe). **A Tier 2 miss
#    is the expensive one**: `dismiss_blocking_dialog` reads None as "closed
#    cleanly" (`stop_reason = "closed"` -> `return True`), and so it reports
#    success against a dialog it never even touched. That is a silent wrong
#    result, not just a missed detection.
#
# And the site's paywall happens to be the shape most likely to exceed the cap:
# **the entire pricing table** (plan cards + a feature comparison table). The 260
# `[blocked] dialog text:` entries in `WEBRunner.log` over 08-24 -> 09-09 had
# only one distinct content, with the tail cut off at the comparison table's
# first row -- that is the `slice(0, 400)` truncation, and the full-text length
# was never measured by anyone. The margin is printed now, see `_TIER1_TEXT_CAP`.
#
# **The return value is deliberately kept as `str | None`** (see
# `has_blocking_dialog`), so the full-text length is carried out in-band: on
# truncation a marker is appended to the tail. The caller uses it only for an
# **equality comparison** (the dismiss-dialog loop's progress criterion) and for
# the log, neither of which is affected; incidentally it also lets the two dialog
# layers "same first 400 chars, different total length" be told apart. `EXCERPT`
# uses a named constant rather than a literal so that "whether there is a length
# cap" remains visible at a glance at the source level (the guard test extracts
# the `text.length > <number>` shape).
_BLOCKING_DIALOG_JS = _JS_VISIBLE + r"""
const EXCERPT = 400;
for (const node of document.querySelectorAll(
       '[role="dialog"],[role="alertdialog"],[aria-modal="true"]')) {
  if (!onScreen(node)) continue;
  const text = (node.innerText || node.textContent || '')
    .replace(/\s+/g, ' ').trim();
  if (!text) continue;
  return text.length > EXCERPT
    ? text.slice(0, EXCERPT) + ' …[truncated; full length ' + text.length + ']'
    : text;
}
return null;
"""

# Whether `snap()` writes `debug_*.png` at all. Read ONCE at this module's
# import (NOT hot-reloaded per character) — `snap` is called from the setup
# phase too, and toggling mid-run would give inconsistent before/after states.
# Toggle in `batch_config.json` then `!stop` + `!run` to apply. (Same single-
# read-at-import semantics as before; now shared by both variants.)
_DEBUG_SCREENSHOTS = bool(load_batch_config().get("debug_screenshots", False))

# DOM diagnostics JS — snapshots every textarea / contenteditable's key attrs.
_DOM_DIAG_JS = r"""
return Array.from(document.querySelectorAll(
    'textarea, [contenteditable="true"]'
)).map((e, i) => {
    const isInput = (e.tagName === 'TEXTAREA' || e.tagName === 'INPUT');
    const fullText = isInput ? (e.value || '') : (e.innerText || '');
    const parentText = (e.parentElement
        ? (e.parentElement.innerText || '') : '');
    return {
        index: i,
        tag: e.tagName,
        aria_label: e.getAttribute('aria-label'),
        placeholder: e.getAttribute('placeholder')
            || e.getAttribute('data-placeholder'),
        visible: !!e.offsetParent,
        value_len: fullText.length,
        value_preview: fullText.substring(0, 60),
        parent_text: parentText.substring(0, 120).replace(/\s+/g, ' ').trim()
    };
});
"""


# ---------- "Which version of the code am I running right now" (see `_code_fingerprint`) -------------

def log_code_fingerprint() -> str:
    """Startup banner: freeze the code fingerprint loaded **right now** and print
    a one-line summary. Returns that line.

    Both variants call it once at the very front of `main()` (after all imports
    have run). It lives in the shared module rather than each duplicating two
    lines, for exactly the same reason as `log_driver_versions`: that the two
    variants must stay in sync is a hard rule of this project, and "only one side
    has the log" means that the one time it matters, the variant running happens
    to be the one without the log.

    **Why it must sample at startup**: only after `_code_fingerprint.snapshot()`
    is there anything to compare against disk. Compute it only when you go to
    investigate, and what you measure is the current disk state -- i.e. the other
    side of the comparison -- so it always reports "no drift" and tests all
    green. That is that module's one real way to fail.

    **No need to worry about later lazy imports.** `_project_source_files()` walks
    `sys.modules`, so a project module imported only after this point shows up in
    `added` -- but `added` **does not count as drift** (see
    `_code_fingerprint.drift_report`: that module was just loaded from disk, it is
    the newest, precisely "not behind"). So there is no "the caller must guarantee
    no further imports" constraint here; lazy-import whatever you like inside a
    function.

    **What this one line is worth**: this project's processes run for days at a
    time (the webrunner has run 78.7 hours straight), while the repo is
    continuously edited as they run. Reading the log afterwards, two questions
    only it can answer -- whether the source text a traceback prints can be
    trusted (`linecache` reads disk at print time, while the line numbers come
    from the code object loaded at load time, so once the file has changed they
    do not line up), and "did that fix actually take effect in this process"
    (the webrunner restarts the browser when it switches characters, not the
    process; `/version` reports git HEAD, but the working tree can carry days of
    uncommitted changes).

    Never raises: this is a one-line diagnostic record, and must have no chance of
    turning a normal startup into a failure.
    """
    try:
        _code_fingerprint.snapshot()
        line = _code_fingerprint.describe()
    except Exception as error:  # pylint: disable=broad-except
        # `!r` is kept on purpose: the `try` only touches `_code_fingerprint` (a
        # pure file hash), which cannot reach the driver; whereas `str(OSError)`
        # would print the hashed source paths.
        line = f"code fingerprint unavailable: {error!r}"
        print(f"code fingerprint failed: {error!r}", file=sys.stderr)
    print(f"  [fingerprint] {line}")
    return line


def report_code_drift() -> str:
    """Periodic checkpoint (character boundary): whether the code on disk has
    diverged from what I loaded at startup.

    **When `drifted is False`, print not one word.** This is not a style
    preference, it is this function's most important property: drift is this
    project's **normal state** (the repo is edited constantly), every character
    passes a character boundary once, and `discord_bot.log` already has the
    precedent of 11,250/11,746 lines all being the same `rpc apply ->` line -- a
    log that gets turned off is as good as no log. So speak only when there is
    genuinely something to say.

    `None` (cannot tell) must also speak, but its wording must be distinct from
    "there is drift": this project has hit "a failed scan looks exactly like a
    clean scan" once each on `_find_all_chrome_processes` / `_load_pid` /
    `dashboard_server`, and does not repeat it here.

    **Detecting drift changes no behaviour** -- no restart, no abort, no refusal
    to generate. Drift is the normal state, and wiring it into control flow only
    manufactures false kills. Purely diagnostic; returns the line it printed
    (empty string if it printed nothing).

    Never raises, same reason as `log_code_fingerprint`.
    """
    try:
        report = _code_fingerprint.drift_report()
    except Exception as error:  # pylint: disable=broad-except
        # `!r` is kept on purpose: same as above, `_code_fingerprint` is a pure
        # module and cannot reach the driver.
        print(f"code drift check failed: {error!r}", file=sys.stderr)
        return ""
    if report["drifted"] is False:
        return ""  # normal, pass silently -- do not manufacture the next `rpc apply ->`.
    if report["drifted"] is None:
        line = f"  [fingerprint] cannot tell whether the code changed: {report['why']}"
        print(line)
        return line
    # List only the two kinds that really mean "I am behind". `added` **does not
    # count as drift** (see `_code_fingerprint.drift_report`: it is a module
    # lazy-imported only after `snapshot()`, just loaded from disk and thus the
    # newest), so mixing it into this line would only point at a filename with no
    # problem, sending the log reader in the wrong direction. `describe()` makes
    # the same trade-off.
    names = report["changed"] + report["removed"]
    line = (f"  [fingerprint] the code on disk is now different from what this "
            f"process loaded at startup "
            f"({report['at_start']} -> {report['now']}); "
            f"**this process is still running the old version**, and will not "
            f"switch until it restarts. "
            f"Changed: {', '.join(names)}")
    print(line)
    return line


# ---------- critical_error diagnostic content ----------------------------------------
# The directory this module lives in. Use the **package** root, not
# `PROJECT_ROOT`: `.venv/` sits under `PROJECT_ROOT`, so comparing against the
# project root would recognise every site-packages frame as "ours", and the
# summary would lose all meaning.
_PACKAGE_ROOT = Path(__file__).resolve().parent

# The budget for `critical_error.traceback`. The criterion is "**every traceback
# ever measured must fit whole**", not "roughly enough" -- the reason this field
# exists is post-hoc reading, and the one that gets truncated is exactly the one
# that needed it. All 8 in `WEBRunner.log` (including the exception chain of the
# three 09-07 `MaxRetryError`s, 7,452 chars raw), after folding, are at most
# **3,575** chars (that one is almost entirely our own frames, with little to
# fold). 4,000 leaves 12% margin while still blocking runaway recursion (a
# `RecursionError` produces thousands of frames).
#
# Larger does not hurt: `critical_error` measured 7 times in 76 days, and
# `events.ndjson` is rotated by the bot. **This number must track measurement**
# -- measure it with `_traceback_excerpt(raw, limit=10**9)`.
_TRACEBACK_BUDGET = 4000


def _traceback_excerpt(text: str, *, limit: int = _TRACEBACK_BUDGET) -> str:
    """Condense a traceback into a version where **our own frames are always
    kept**.

    **It used to be `traceback.format_exc()[-1500:]`, and that throws away the
    useful part on exactly the class of failure that needs it most.** Measured
    over all 7 `critical_error`s: when the exception comes from our own code, the
    trailing 1500 chars contain 2-5 of our frames and 0 third-party; when it
    comes from deep in a library (the three 09-07 `MaxRetryError`s) it is **0 of
    ours, 4 of urllib3's**.

    **And "keep the head instead" does not fix it either.** Same measurement: our
    frames land at 2564-4025 within 7,452 chars, **neither head nor tail**. The
    reason is the exception chain -- Python prints the innermost cause first
    (urllib3's `_new_conn` -> `ConnectionRefusedError`), and our frame is at the
    start of the **third** traceback section. So this does not truncate head/tail,
    it filters by **origin**:

    * Unindented lines (`Traceback (most recent call last):`, `During handling of
      the above exception…`, the final exception line) are always kept -- the
      skeleton of the exception chain and the final cause of death.
    * A `File "…"` frame under this package's directory, together with its source
      echo, is always kept.
    * A run of consecutive third-party frames keeps only the **first** (that is
      the one where we hand off, and it names which library), and the rest fold
      into one line `[... omitted N third-party frames ...]`. urllib3's retry is
      recursive, so that run is nearly identical anyway.
    * Only last does the overall cap apply, keeping both head and tail and marking
      how many chars were omitted in the middle -- that is the anti-recursion-blowup
      fuse, not the main mechanism.

    Reading reminder: the **source text** in this field lies once the file has
    been changed (`linecache` reads disk at print time, while the line numbers
    come from the code object loaded at load time). So the same event also carries
    `code_drift` -- see `code_drift_flag`.
    """
    kept: list[str] = []
    marker = str(_PACKAGE_ROOT)
    foreign_frames = 0
    in_our_frame = True          # if unsure, treat it as "ours" -> rather keep it

    def flush() -> None:
        nonlocal foreign_frames
        if foreign_frames > 1:
            kept.append(f"  [... omitted {foreign_frames - 1} third-party frames ...]")
        foreign_frames = 0

    for line in text.splitlines():
        if line[:1] not in (" ", "\t"):
            flush()
            kept.append(line)
            in_our_frame = True
            continue
        if line.lstrip().startswith('File "'):
            if marker in line:
                flush()
                in_our_frame = True
                kept.append(line)
            else:
                in_our_frame = False
                foreign_frames += 1
                if foreign_frames == 1:
                    kept.append(line)
            continue
        # The source echo beneath a frame: kept only for **our own** frames. A
        # third-party frame's echo takes non-trivial space (Python 3.11+'s
        # `...<10 lines>...` block is easily two hundred chars), and the question
        # it answers -- which library, which function -- is already answered by
        # the `File` line above it.
        if in_our_frame:
            kept.append(line)
    flush()

    out = "\n".join(kept)
    if len(out) <= limit:
        return out
    # The fuse. The tail must be kept: the traceback's **last line** is the
    # exception type and message.
    head = limit * 2 // 3
    tail = limit - head
    return (out[:head]
            + f"\n  [... a further {len(out) - head - tail} chars omitted in the middle ...]\n"
            + out[-tail:])


def code_drift_flag() -> bool | None:
    """Whether the code on disk has changed since startup (True / False / None =
    cannot tell).

    Only for the `critical_error` event, for the reason of a reading trap
    actually hit: the **source text** a traceback prints is read from disk at
    print time (`linecache`), while the line numbers come from the code object
    loaded at load time -- once the file has changed, the two do not line up. The
    2026-09-07 17:10 entry is an instance: the frame said `_note_transport_error`,
    but the text printed was `class BrowserGoneError(RuntimeError):`. Without this
    flag, whoever reads `events.ndjson` has no way to tell whether that
    traceback's text can be trusted (`report_code_drift()` only prints to the log,
    and the log gets rotated away).

    **Reading rule: line numbers are always trustworthy; source text is
    trustworthy only when `code_drift` is False.**

    Never raises: this is a diagnostic field on the death path, and must not
    manufacture a second exception.
    """
    try:
        return _code_fingerprint.drift_report()["drifted"]
    except Exception:  # pylint: disable=broad-except
        return None


# ---------- The "a batch is running" liveness signal -----------------------------------------

def claim_liveness_signal() -> int | None:
    r"""If nobody has claimed `webrunner.pid`, write our own pid into it. Returns
    the claimed pid, otherwise None.

    Normally this file is written by the **parent process**: the bot's
    `_spawn_webrunner` and `start_webrunner.py` write the pid inside the short
    critical section of the spawn (holding the Chrome slot) before releasing the
    slot, after which the file serves as the long-lived "a batch is running"
    signal for the whole run. ※ **The Chrome slot is not that signal** -- it is
    only the millisecond-scale critical section around the spawn (measured: the
    batch ran for days on end, and `chrome_slot.lock` never existed).

    **A bare run (`py -3 axiomatic/webrunner_novelai.py`) has no such parent
    process, so neither signal exists.** The consequence is not only "a
    concurrent `verify_browser` opens its own browser and is then killed at the
    next character boundary by `_kill_orphan_chrome`'s machine-wide sweep" (a
    symptom that looks like the browser breaking on its own, while the cause is in
    another process) -- worse, `start_webrunner.py`'s and `run_batch.py`'s "do not
    start another if a batch is already running" fail too, and so **two**
    webrunners start on the same machine, contending for one
    `.chrome_profile_snap/`, nuclear-sweeping each other, and re-picking from the
    same `todo_*.md`. And the boot task `install_autostart.py` registers runs
    exactly `start_webrunner.py`, so that path is **reachable unattended**.

    So what this adds is "**claim it yourself if nobody has**", not "overwrite
    unconditionally".

    ※ **The race with the parent process is benign, both orders are correct.**
    The parent writes only **after `Popen` returns** (`_supervisor.stream_child`:
    `Popen` first, then `on_spawn`), so the child really does have a chance to
    write first -- measured, `Popen` returns in only 7ms while the child has to
    start the interpreter first and then import the driver, so in practice the
    parent always writes first, but do not rely on that. If it really is the other
    way round, the file holds the child's own pid, **which is likewise a live
    process, and precisely this batch's**, and **the gatekeeping readers only ask
    "is a batch running"**, not whether that pid was written by the parent or the
    child. On cleanup both sides delete only a file that "still records their own
    entry" (see `release_liveness_signal` and
    `start_webrunner._clear_pid_if_ours`), so neither will wrongly delete the
    other's signal.

    ※ **That sentence used to say "four readers", and that number was both stale
    and untrue even for this function itself (fixed 2026-09-21).** Scanning the
    whole product code by AST, **eight** functions read `webrunner.pid`, this one
    among them -- but it does not ask "is a batch running", it asks "do I need to
    claim this signal", so it was never within that sentence's scope. So the
    number is no longer cited here, only the **gatekeeping** kind is described.
    The full classification (four using the three-way test / four deliberately not
    using it, each with its reason) and its two-way reconciliation live in
    `axiomatic/test_pid_file_readers.py`: a function scanned but not classified
    goes red, and a function left in the list that no longer reads this file goes
    red too.

    Note the parent writes the pid of the **redirector shell**, not the
    webrunner's own `os.getpid()` (`.venv\Scripts\python.exe` is a redirector
    stub, and measured the parent and child pids differ). Both values are valid
    liveness signals, so what is compared here is "alive or not", not "is it me".

    ※ **When it cannot tell, treat it as "someone is running"**
    (`_chrome_slot._pid_alive` returns True when it cannot tell, which is exactly
    the direction wanted here): misjudging as "nobody is running" would
    **overwrite someone else's valid signal**, and that signal is the sole basis
    for every downstream "do not open a second browser" decision; misjudging as
    "someone is running" only means this one bare run publishes no signal,
    returning to the pre-fix state. Using `_chrome_slot`'s copy rather than
    `_process_control`'s is because the latter returns False when it cannot tell
    -- `CLAUDE.md` records that divergence as deliberate, and which copy to use
    depends on "which way the mistake should fall". Also deliberately **do not
    write a fourth copy** of `_pid_alive`.
    """
    try:
        raw = WEBRUNNER_PID_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raw = ""
    except (OSError, UnicodeDecodeError):
        print("cannot read webrunner.pid; conservatively not claiming the liveness signal", file=sys.stderr)
        return None
    if raw:
        try:
            other = int(raw)
        except ValueError:
            print("webrunner.pid's content is not a number; conservatively not claiming the liveness signal",
                  file=sys.stderr)
            return None
        if other == os.getpid() or _chrome_slot._pid_alive(other):
            return None          # the parent (or another batch) already claimed it -> no-op
    mine = os.getpid()
    if not _run_progress.atomic_write_text(WEBRUNNER_PID_FILE, str(mine)):
        print("cannot write webrunner.pid; no liveness signal this run", file=sys.stderr)
        return None
    print(f"  [liveness] no parent process published a liveness signal, this process claims it itself (pid={mine})")
    return mine


def release_liveness_signal(claimed: int | None) -> None:
    """Cleanup: delete **only while the file still records `claimed`**. Never
    raises.

    Same criterion as `start_webrunner._clear_pid_if_ours`: the parent writes the
    same file too, so an unconditional delete would clear **someone else's**
    liveness signal, and then the verify side would open a second browser while
    the batch is still running.
    """
    if claimed is None:
        return
    try:
        raw = WEBRUNNER_PID_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return
    if raw != str(claimed):
        return                   # overwritten by the parent -> that entry is its, let it clean up
    try:
        WEBRUNNER_PID_FILE.unlink()
    except OSError:
        pass


def run_with_liveness_signal(entry) -> int:
    """Run `entry()`, doing its best to ensure a "a batch is running" signal is on
    disk throughout.

    Hooked into both variants' `__main__` rather than inside `main()`: it covers
    the whole process (including `_kill_orphan_chrome` and the dozen-plus seconds
    of building the driver -- precisely the window the verify side is most likely
    to squeeze into), `main()` need not be indented as a whole, and **isolated
    verification mode takes another branch** (`_run_setup_verification`), so it
    inherently does not claim a pid.

    ※ **A failed claim must never block the batch.** This is a purely side-channel
    signal; if it has a bug, the worst outcome should be "back to the pre-fix
    state, no signal", not the webrunner failing to start -- which would turn into
    the supervisor respawning endlessly and rapid-fail giving up, i.e. **trading
    the whole batch for a reliability improvement**. So the claim is wrapped in a
    broad except (the cleanup half already promises never to raise).
    """
    try:
        claimed = claim_liveness_signal()
    except Exception as error:  # pylint: disable=broad-except
        # `!r` is kept on purpose: `claim_liveness_signal` is file I/O + pid
        # probing, which cannot reach the driver; `str(OSError)` would carry out
        # the liveness-signal file's full path.
        print(f"error while claiming the liveness signal, skipped (does not affect the batch): {error!r}",
              file=sys.stderr)
        claimed = None
    try:
        return entry()
    finally:
        release_liveness_signal(claimed)


# ---------- events -----------------------------------------------------------

def emit_event(event_type: str, **data) -> None:
    """Append a structured event to events.ndjson for the Discord bot watcher.

    **Telemetry must never interrupt the batch.** This is a best-effort side
    channel: if it cannot write, one notification is missing, and letting an
    unattended batch die here is a completely disproportionate cost (same as
    `dorossi_backend._dorossi_record_usage`'s "recording usage must never
    interrupt a conversation round").

    So the except cannot catch only `OSError` -- a `json.dumps` failure is not an
    OSError at all:

    * `TypeError` -- an unserialisable value (`Path`, `datetime`, `set`,
      selenium's `WebElement`). Currently **every** call site passes
      str/int/float/bool/`list[str]` (checked one by one), so this is a latent,
      not a live, bug. The most likely one to hit first is `check_dom_request`'s
      `data=diag`: that is **data returned from the browser**, and
      `dump_textareas_diag` only checks the outer `isinstance(result, list)`, not
      the elements, so the day `_DOM_DIAG_JS` returns one more DOM node it becomes
      a `WebElement`.
    * `ValueError` -- a circular reference; **and `UnicodeEncodeError` (a subclass
      of ValueError)**, which hits when a string returned from the browser
      carries a lone surrogate, far more realistic than a circular reference.

    Serialise first, open the file second: an unserialisable value then leaves no
    empty file behind, and a line is either written whole or not at all -- the
    reader (the bot's watcher) never reads half a line.
    """
    record = {"ts": time.time(), "type": event_type, **data}
    try:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with EVENTS_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except (OSError, TypeError, ValueError) as error:
        # `!r` is kept on purpose: all three exceptions have non-empty `args`,
        # and `str(OSError)` would write `events.ndjson`'s full host path into the
        # log (`/log tail` would send it out).
        print(f"emit_event({event_type}) failed: {error!r}", file=sys.stderr)


def wait_if_paused(label: str = "") -> None:
    """Block at safe batch boundaries while the bot's pause marker exists."""
    def _read_pause() -> dict | None:
        try:
            raw = WEBRUNNER_PAUSE_FILE.read_text(encoding="utf-8").strip()
            parsed = json.loads(raw) if raw else {"mode": "now"}
            return parsed if isinstance(parsed, dict) else {"mode": "now"}
        except Exception:  # pylint: disable=broad-except
            return {"mode": "now"}

    def _write_pause(data: dict) -> None:
        # Atomic write (the hard rule for cross-process files). The bot side has
        # always written this file atomically, but this side did an in-place
        # overwrite -- a half-written JSON makes `_read_pause` fail to parse and
        # fall back to `{"mode": "now"}`, i.e. it turns "stop after this image"
        # into "stop now".
        _run_progress.atomic_write_text(
            WEBRUNNER_PAUSE_FILE, json.dumps(data, ensure_ascii=False))

    is_pair_boundary = "pair" in (label or "").lower()
    announced = False
    while True:
        marker = _read_pause() if WEBRUNNER_PAUSE_FILE.exists() else None
        if marker is None:
            break
        mode = str(marker.get("mode") or "now").lower()
        if mode == "after_current":
            if is_pair_boundary:
                marker["mode"] = "now"
                _write_pause(marker)
                mode = "now"
            else:
                break
        elif mode == "after_pairs":
            if is_pair_boundary:
                remaining = marker.get("remaining", 0)
                if not isinstance(remaining, int):
                    remaining = 0
                if remaining > 0:
                    marker["remaining"] = remaining - 1
                    _write_pause(marker)
                    break
                marker["mode"] = "now"
                _write_pause(marker)
                mode = "now"
            else:
                break
        if mode != "now":
            break
        if not announced:
            print(f"  paused by bot{f' ({label})' if label else ''}; "
                  f"waiting for resume marker removal")
            emit_event("paused", label=label)
            announced = True
        time.sleep(2.0)
    if announced:
        print("  pause cleared; resuming")
        emit_event("resumed", label=label)


# ---------- DOM diagnostics (formatting only — the capture stays per-variant
# because it calls execute_script / jss) ------------------------------------

def format_textareas_diag(diag: list[dict]) -> str:
    """Expand the dump_textareas_diag result into a multi-line string for
    webrunner.log to print."""
    if not diag:
        return "DOM diag: (no textareas / contenteditable found)"
    lines = [f"DOM diag: {len(diag)} text-fields"]
    for t in diag:
        idx = t.get("index", "?")
        tag = t.get("tag", "?")
        vis = "Y" if t.get("visible") else "N"
        n = t.get("value_len", 0)
        aria = t.get("aria_label") or ""
        ph = (t.get("placeholder") or "")[:40]
        preview = (t.get("value_preview") or "")[:40]
        parent = (t.get("parent_text") or "")[:60]
        lines.append(
            f"  [{idx}] {tag} vis={vis} len={n} "
            f"aria={aria!r} placeholder={ph!r} "
            f"preview={preview!r} parent={parent!r}"
        )
    return "\n".join(lines)


# ---------- single-image (one-shot) path helper -----------------------------

def _single_image_relative_path(save_path: Path) -> str:
    """Convert a one-shot image's absolute path into a PROJECT_ROOT-relative
    POSIX string (a contract requirement, e.g.
    `output/_oneshot/<request_id>/<file>.png`). If making it relative fails, fall
    back to the filename itself (still a valid POSIX segment); never throws."""
    try:
        return save_path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except (ValueError, OSError):
        return save_path.name


# ---------- Browser window hiding (shared by both variants) ---------------------------------
#
# The two webrunners each originally had a **line-for-line identical**
# implementation, both hooking `win32gui` / `win32process` themselves: scan
# psutil for browser processes whose cmdline contains the profile path ->
# enumerate windows -> match windows to their owning process -> minimise. One
# thing, two implementations, and only ever one of them gets fixed, so it is
# consolidated into one copy here, with the Win32 parts all delegated to the
# desktop-automation library.
#
# Why find by **process** rather than window title: the browser is
# multi-process, the window title is the current page's title (constantly
# changing), and several processes have no window at all. The owner is the stable
# key.

def find_browser_pids_for_profile(profile_dirs: list[Path]) -> set[int]:
    """The pids of browser processes whose cmdline points at these profile
    directories.

    Before matching, normalise paths to lowercase and forward slashes -- the
    spelling in cmdline is not necessarily consistent with `Path`'s string form
    (backslash vs forward slash, case), and a direct string comparison would
    miss.

    **The match is a boundary match, not a substring match.** This project has
    both `.chrome_profile` and `.chrome_profile_snap` (the former is the source
    used for login, the latter the copy Chrome actually opens), and the former is
    a **strict prefix** of the latter, so `path in cmd` would count a process
    running on the snapshot as `.chrome_profile`'s too. It is more obvious once
    isolated verification mode swaps `CHROME_PROFILE_SNAPSHOT` for
    `.chrome_profile_verify`: the wanted set
    `[.chrome_profile, .chrome_profile_verify]` would **select the production
    batch's browser as well**. The only caller right now is
    `hide_browser_windows` (a mismatch only moves someone else's window
    off-screen), but this function's contract is "which processes belong to this
    profile", and the day someone wires it into the terminate side, over-matching
    means **killing a process that should not be killed** -- this repo has already
    lost a 78.7-hour batch to "killing too many". So tighten it at the source
    rather than requiring every caller to be careful.
    """
    try:
        import psutil  # type: ignore
    except ImportError:
        return set()
    # A boundary = end of string, a path separator (backslashes already
    # normalised to `/`), whitespace or a quote. psutil's `cmdline()` returns an
    # **already-split arg list**, so in practice what follows the target path is
    # only "this arg ends here" (-> whitespace or end) or "there is a deeper
    # level" (-> `/`); the quote is left for the few driver versions that return
    # the whole command line verbatim. `_` and alphanumerics are deliberately
    # **not** in the boundary set -- a `_snap` / `_verify` suffix is exactly what
    # is to be blocked.
    wanted = [
        re.compile(re.escape(str(path).replace("\\", "/").lower())
                   + r"""(?=$|[/\s"'])""")
        for path in profile_dirs
    ]
    if not wanted:
        return set()
    found: set[int] = set()
    try:
        # `attrs=` takes only the cheap `name`, and `cmdline()` is fetched one by
        # one for the few that pass the filter. Putting it in `attrs=` reads the
        # PEB once for **every process on the machine**, nine-tenths of which are
        # discarded by the next line's name check. Measured locally (361
        # processes, 13 browser processes): fetching together 334 ms, filter
        # first then fetch 121 ms. This runs once every time the browser
        # restarts, and `restart_chrome_every_n_characters` defaults to 1 = once
        # per character.
        for proc in psutil.process_iter(attrs=["pid", "name"]):
            try:
                if (proc.info.get("name") or "").lower() != "chrome.exe":
                    continue
                cmd = " ".join(
                    arg for arg in (proc.cmdline() or [])
                    if isinstance(arg, str)
                ).replace("\\", "/").lower()
                if any(pattern.search(cmd) for pattern in wanted):
                    found.add(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as error:  # pylint: disable=broad-except
        # `!r` is kept on purpose: the `try` only touches psutil, cannot reach the
        # driver, and psutil's exceptions (including
        # `RuntimeError: SystemExtendedHandleInformation buffer too big`) have
        # non-empty `args`, so `repr()` can read the message.
        print(f"find_browser_pids_for_profile failed: {error!r}", file=sys.stderr)
    return found


# Always place the browser window where no screen can see it, and **do not
# minimise** (owner's request 2026-09-22: the in-progress image preview must
# never be visible in the foreground at any time).
#
# The original approach opened the window with `--start-maximized`, minimised it
# only after setup, and minimised again between images "in case a click or focus
# woke it back up". That approach was itself the source of the symptom: the
# window really did get woken to the foreground and only then shrunk, and in that
# instant the just-generated image was visible; and the minimise/restore system
# animation also scales the window content out from the taskbar button. Changed
# to placing it at (-32000, -32000) at open time, so afterwards no matter what
# "wakes", "restores" or "brings it to front", it is still at that coordinate --
# measured on this machine 2026-09-22: clicking an element, screenshotting,
# `ShowWindow(SW_RESTORE)` + `SetForegroundWindow`, minimising then restoring,
# CDP `Page.bringToFront` -- the window stayed off-screen the whole time and the
# frame kept rendering; `--window-position` also overrides the window position
# remembered in the profile (even "last time it was maximised"). Rendering did
# not stop thanks to the three flags already in `_MEMORY_FLAGS`
# (`--disable-backgrounding-occluded-windows` etc.) -- an off-screen window is, to
# Chrome, an occluded window.
#
# The size is fixed at 1920x1080 rather than following the screen: the content
# area when maximised was roughly this size originally, and fixing it keeps the
# page layout from changing with the host's screen resolution (the DOM flow is
# written against this layout).
OFFSCREEN_WINDOW_POSITION = (-32000, -32000)
OFFSCREEN_WINDOW_SIZE = (1920, 1080)
# If both top-left coordinates are no greater than this value, treat it as
# already off-screen. Multi-screen layout coordinates are actually within a few
# thousand, so this threshold will not count any real screen as "off-screen"; nor
# does it have to equal exactly -32000 -- if the system or browser nudges it a few
# pixels, there is no need to move it again.
_OFFSCREEN_EDGE = -20000


def hide_browser_windows(profile_dirs: list[Path]) -> tuple[int, int]:
    """Move those profiles' browser windows back off-screen; returns
    `(windows hidden, windows still on screen)`.

    **Does not minimise, and does not restore.** An already-minimised window is
    invisible anyway, and restoring it would instead play an animation scaling the
    content out of the taskbar, so it is left as is; when it is later restored it
    returns to "the position before minimising" = off-screen. Moving uses
    `MoveWindow`, which does not bring the window to front and does not steal
    focus.

    `(0, 0)` means no window was found at all (the process has not started yet, or
    the desktop-automation library is unavailable) -- the caller must not read it
    as "hidden".
    """
    pids = find_browser_pids_for_profile(profile_dirs)
    if not pids:
        return 0, 0
    try:
        import je_auto_control as ac  # type: ignore
        wm = ac.windows_window_manage
    except Exception as error:  # pylint: disable=broad-except
        # `!r` is kept on purpose: the `try` only touches the desktop-automation
        # library's import, and what it catches is `ImportError` /
        # `AttributeError` (non-empty `args`), unrelated to selenium.
        print(f"hide_browser_windows: backend unavailable: {error!r}",
              file=sys.stderr)
        return 0, 0
    x, y = OFFSCREEN_WINDOW_POSITION
    hidden = exposed = 0
    for pid in pids:
        try:
            for hwnd, _title in ac.windows_for_process_id(pid):
                if wm.is_window_minimized(hwnd):
                    hidden += 1
                    continue
                rect = wm.get_window_rect(hwnd)
                if rect is None:
                    exposed += 1
                    continue
                left, top, right, bottom = rect
                if left <= _OFFSCREEN_EDGE and top <= _OFFSCREEN_EDGE:
                    hidden += 1
                elif wm.move_window(hwnd, x, y, right - left, bottom - top):
                    hidden += 1
                else:
                    exposed += 1
        except Exception as error:  # pylint: disable=broad-except
            # `!r` is kept on purpose: this is the desktop-automation library's exception, not selenium's.
            print(f"hide_browser_windows({pid}) failed: {error!r}",
                  file=sys.stderr)
            exposed += 1
    return hidden, exposed


# ---------- chromedriver's log (the only source of a spawn failure's root cause) ---------
#
# **The two variants share one copy instead of each keeping its own.** This whole group is
# pure `Path` work that never touches the driver, and the measurements it records (growth
# rate, the basis for the cap, why a mid-session trim gets zero-filled) were paid for by
# measuring — copied twice, the next re-measurement would update only one of them. The same
# reasoning already moved `hide_browser_windows` / `find_browser_pids_for_profile` here.
#
# **The je variant was only wired up on 2026-09-09.** Before that it had none of this, so
# when `/run` (the bot's default variant is je) hit a Chrome that would not start, all it got
# was a guess — "likely Out of Memory or a chromedriver/Chrome version mismatch" — and once
# the 5-minute startup window passed, `_watch_for_fallback` quietly switched to the selenium
# variant, and the user never even asked "why did je die". The je side **can** produce this
# file: `wr.set_driver` passes its `**kwargs` untouched to `webdriver.Chrome(...)`, so passing
# a `service=ChromeService(log_output=…)` is enough (measured in `test_je_facade.py`).

_CHROMEDRIVER_LOG = PROJECT_ROOT / "chromedriver.log"
# The previous driver session's log. See `_rotate_chromedriver_log`.
_CHROMEDRIVER_LOG_PREV = PROJECT_ROOT / "chromedriver.prev.log"
# The cap on `chromedriver.log` (see `_trim_chromedriver_log`).
#
# **The 64 MB trigger threshold is based on "how much one character's session actually
# writes"**, re-measured 2026-09-07:
# - This file's size depends on **how long a single chromedriver session is** (it is only
#   cleared at startup). Measured on a session in progress: started 19:18:11, last entry
#   00:56:41, 5.64 hours wrote 12,747,290 bytes = **2.15 MB/hour (37 KB/minute)**.
# - Session length = `images_per_character` ÷ the **actual** image rate. Measured rate 8.0
#   images/hour (counting the mtimes of `output/**/*.png` over 13 full hours), with
#   `images_per_character=120` → **one character is 15.0 hours ≈ 28–32 MB**.
# - So the threshold is 64 MB = twice that amount: a healthy run never triggers it, while
#   "one session running as long as several characters" (`restart_chrome_every_n_characters=0`,
#   or the rate dropping to a fraction) is still caught.
#
# **The earlier 8 MB was wrong; recorded here so it is not repeated**: its basis was "above the
# normal single-character amount of 5.4 MB", but that 5.4 MB was the amount **partway through**
# a character, not at its end. As a result every character triggered the warning once —
# exactly the crying wolf it was meant to avoid. Taking "halfway through" for "finished" is the
# real lesson here. Incidentally it is no longer aligned with the two launchers'
# `LOG_MAX_BYTES`: those govern line-oriented logs that people read, with an entirely different
# growth curve, and "one number across the project is easy to remember" is no reason to pick a
# threshold.
#
# **Keeping 256 KB is based on "how much it takes to reconstruct one failure", and is not
# affected by the recalculation above:**
# - One complete spawn-failure scene measured **1,090 bytes** at `--log-level=INFO`
#   (`Starting ChromeDriver` + the `COMMAND InitSession` carrying every Chrome flag and
#   `--user-data-dir` + `RESPONSE InitSession ERROR session not created`, plus the reason from
#   Chrome's side). 256 KB is 240 times that.
# - The only reader, `_read_tail_text`, reads at most **64 KiB** at a time, so the amount kept
#   must be ≥ that window, or after a trim the tail read gets a truncated sliver. 256 KB = 4x.
# - A post-crash reconciliation wants "the last few WebDriver commands before it died". At the
#   37 KB/minute re-measured above, 256 KB ≈ the last 7 minutes, enough to see the cause.
_CHROMEDRIVER_LOG_MAX_BYTES = 64 * 1024 * 1024
_CHROMEDRIVER_LOG_KEEP_BYTES = 256 * 1024


def _read_tail_text(path: Path, max_bytes: int = 64 * 1024) -> str:
    """Read at most `max_bytes` of text from the **end** of a file, without loading the whole
    file into memory.

    `read_text()` reads the whole thing. chromedriver's log can grow to tens of MB within one
    character (measured 2026-08-29: about 484 bytes per WebDriver command at
    `--log-level=INFO`, about 4370 with `--verbose`), and this function is called precisely
    when **Chrome has just failed to start** — which is often exactly when memory is short.
    Only the last few lines matter; there is no reason to pay for the whole file.

    Cutting in the middle usually leaves a half line first, so after a seek the first line is
    dropped.
    """
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        offset = max(0, size - max_bytes)
        handle.seek(offset)
        raw = handle.read()
    text = raw.decode("utf-8", errors="replace")
    if offset and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def _dump_chromedriver_log_tail(lines: int = 25) -> None:
    """Print the tail of `chromedriver.log` to stderr. Selenium's
    `SessionNotCreatedException: Chrome instance exited` carries no root
    cause — the verbose chromedriver log does (version mismatch, profile
    lock, OOM at launch …). Called when a spawn attempt fails.

    Say so **explicitly** when it cannot be read. This used to be `except OSError: return`, so
    "the log was never written" and "the log is empty" looked exactly like "all is well, just
    nothing to print" — and `ChromeService` was being passed `log_path`, which selenium does
    not recognise at all, so the file was never created. For months the only thing that would
    have exposed this was this helper, and it chose silence.

    **`.prev` is printed as well.** The `chromedriver.log` of this failed spawn only holds the
    messages of "this attempt failed to start"; what one actually wants is often **how the
    previous session died** (the copy `_rotate_chromedriver_log` keeps). Print both, and label
    each — unlabelled, the two sets of timestamps blur together and the reader takes them for
    one session.
    """
    for path, label in ((_CHROMEDRIVER_LOG_PREV, "chromedriver.prev.log (the previous "
                         "session — the one to read for \"died partway through\")"),
                        (_CHROMEDRIVER_LOG, "chromedriver.log (this attempt)")):
        try:
            text = _read_tail_text(path)
        except FileNotFoundError:
            if path is _CHROMEDRIVER_LOG_PREV:
                continue          # the first launch has no earlier copy yet; normal, stay quiet
            print("--- chromedriver.log cannot be read (FileNotFoundError) — "
                  "the driver's detailed log was never written, so this spawn failure has no root "
                  "cause to look at. Check ChromeService's log_output parameter. ---",
                  file=sys.stderr)
            continue
        except OSError as error:
            print(f"--- {label} cannot be read ({type(error).__name__}) ---",
                  file=sys.stderr)
            continue
        tail = text.splitlines()[-lines:]
        if not tail:
            print(f"--- {label} is empty — the driver has not had time to write anything yet ---",
                  file=sys.stderr)
            continue
        print(f"--- {label} (last {len(tail)} lines) ---", file=sys.stderr)
        for line in tail:
            print(line, file=sys.stderr)
        print(f"--- end {label} ---", file=sys.stderr)


def _trim_chromedriver_log() -> None:
    """Cap the `chromedriver.log` left by the previous driver session (keeping the tail).

    **Hard precondition: no chromedriver may be alive at the moment of the call.** This is not
    caution for its own sake; it was measured on 2026-09-06 — `trim_log` goes through
    `write_bytes()` (truncate, then rewrite), while chromedriver holds a file handle whose
    **offset stays where it was**:

        before trim 5,170 bytes → after trim 746 bytes → it writes a few more lines → 7,572
        bytes, of which **4,424 are NUL** (the OS zero-fills the hole in the middle)

    In other words, trimming mid-session not only **reclaims no space** (the file immediately
    grows back larger than before the trim), it also turns what we want to inspect into
    garbage — `_read_tail_text` reads back exactly that zero fill. So this only hangs on the
    seam "the old driver is gone and the new one is not up yet", and that is also why it is
    **not in `main()`'s finally**: the `cur.quit()` there is wrapped in try/except pass, and
    when quit hangs chromedriver is still alive, which is exactly the case above.

    The safety of the trim point is guaranteed by the callers: both production paths into
    `build_stealth_driver()` have just run `_kill_orphan_chrome()` (at boot that is `main()`,
    for periodic restarts `_restart_chrome_session`), which sweeps every `chromedriver.exe` in
    two passes with psutil + `taskkill /F /T /IM`. The verify-mode path does not sweep
    (`_SUPPRESS_ORPHAN_SWEEP`), but `verify_browser.py` first takes the `_chrome_slot` lock and
    yields to a live `webrunner.pid`, so no chromedriver of ours runs there either.

    **Why this layer is still needed even though it looks inert in a healthy run.**
    chromedriver **clears** the file `--log-path` points to on every start (measured
    2026-09-06: padded by hand to 50,368 bytes, back to 366 bytes after the next start), so
    normally the thing actually capping it is chromedriver itself, and the cap equals "one
    chromedriver session's worth". This layer turns that cap into **one we hold ourselves**,
    because there is more than one way for it to vanish, and every one of them is silent:

    1. `selenium.webdriver.common.service.Service.__init__` really does contain
       `if isinstance(log_output, str): self.log_output = open(log_output, "a+")`
       — **append mode, it only ever grows**. The only thing saving us today is
       `ChromiumService.__init__` turning the string into `--log-path=` first (measured:
       `service.log_output` is -3 = DEVNULL). The day that interception goes away, this file
       becomes append-only.
    2. chromedriver itself has `--append-log`. Someone adding it to reconcile across restarts
       gets the same result.
    3. Passing a file object instead of a string also goes through a Python-side handle.
    Also, a failure where chromedriver never started at all (Selenium Manager could not resolve
    it, or the port could not even be bound) does not clear this file, so what
    `_dump_chromedriver_log_tail` reads is the tail of **the previous session** — it looks like
    a diagnosis but is actually stale data.

    What really has **no** cap yet is the growth "within a single session": measured at about
    2.15 MB/hour (2026-09-07: 5.64 hours wrote 12,747,290 bytes). That cannot be trimmed
    mid-session (see above), so all this can do is cap it afterwards + say so.

    **The warning's diagnosis must name the right cause.** File size ≈ single session length ×
    that rate, and session length is `images_per_character` ÷ the **actual** image rate —
    `restart_chrome_every_n_characters` counts in **characters**, not in time, so even set to 1
    it can perfectly well be a session of more than ten hours. That is exactly the case
    measured on this machine: that value = 1, `images_per_character=120`, 8.0 images/hour under
    quota throttling → one character is 15 hours ≈ 30 MB.
    **The earlier warning line saying "most likely restart_chrome_every_n_characters=0" was
    wrong**: following it you find a setting of 1 and then cannot find the problem. `=0` is
    only one of many reasons a session gets long, and it is not this machine's case.

    Incidentally, this file contains **the prompt text actually typed into the page**
    (`RESPONSE ExecuteScript "…"`). It is already in `.gitignore`, but do not paste it around
    casually.
    """
    try:
        size = _CHROMEDRIVER_LOG.stat().st_size
    except OSError:
        return
    if size <= _CHROMEDRIVER_LOG_MAX_BYTES:
        return
    print(
        f"chromedriver.log from the previous session is {size / (1024 * 1024):.1f} MB"
        f" (cap {_CHROMEDRIVER_LOG_MAX_BYTES // (1024 * 1024)} MB), "
        f"keeping only the last {_CHROMEDRIVER_LOG_KEEP_BYTES // 1024} KB. "
        f"This file is only cleared when chromedriver **starts**, so its size ≒ the length of "
        f"a single Chrome session × about 2 MB/hour; and session length = images_per_character "
        f"÷ the **actual** image rate, not the character count in "
        f"restart_chrome_every_n_characters. (Measured: that value = 1, "
        f"images_per_character=120, a rate of 8 images/hour, and one character is still "
        f"15 hours ≒ 30 MB.) To investigate, compute that product first; when something is "
        f"really wrong it is far larger than one character's worth — a session running as long "
        f"as several characters (e.g. restarts disabled), or the rate dropping to a fraction.",
        file=sys.stderr)
    trim_log(_CHROMEDRIVER_LOG,
             max_bytes=_CHROMEDRIVER_LOG_MAX_BYTES,
             keep_bytes=_CHROMEDRIVER_LOG_KEEP_BYTES)


def _rotate_chromedriver_log() -> None:
    """Keep the previous session's `chromedriver.log` as `chromedriver.prev.log`.

    **Without this layer the file never holds evidence of a "died partway through" run.**
    chromedriver clears the file `--log-path` points to on every start, so the sequence is:
    chromedriver dies → webrunner rc=1 → the supervisor respawns → the new chromedriver opens
    the same path and **truncates it**. Measured (2026-09-07): crash at 11:44:54, respawn at
    11:44:59, and the first entry of the `chromedriver.log` on disk was **11:45:02** — 8
    seconds after the crash. The only thing that could have recorded the cause of death was
    overwritten by the next chromedriver, with no copy anywhere.

    **This overturns `_dump_chromedriver_log_tail`'s original rationale.** Its docstring says
    "Selenium's exception cannot name the cause, the verbose chromedriver log can" — true for
    **startup failures** (that log belongs to the failure at hand), entirely false for **dying
    partway through**: the diagnostic tool itself destroyed the evidence its purpose required.

    Its position is exactly the same as `_trim_chromedriver_log`'s, for the same reason: it
    must run **before** `ChromeService(...)`, after which a chromedriver process holds that
    path. The keep step uses `os.replace` (an atomic rename on the same volume), so there is no
    "killed halfway through keeping it" leaving half a copy behind.

    **`.prev` falls under the same disk cap**: each of the two files is at most
    `_CHROMEDRIVER_LOG_MAX_BYTES`, and doubling the combined cap is deliberately accepted — a
    64 MB cap is only ever reached when something is wrong (measured: about 30 MB per
    character), and what the doubling buys is "the next unexplained death has evidence to look
    at". It trims before renaming, so `.prev` always receives an already-capped file.
    """
    try:
        if not _CHROMEDRIVER_LOG.exists():
            return
        os.replace(_CHROMEDRIVER_LOG, _CHROMEDRIVER_LOG_PREV)
    except OSError as error:
        # Purely a diagnostic aid, must never block starting the driver — but it has to speak
        # up, otherwise "no .prev" and "keeping it failed" look exactly alike on disk.
        print(f"failed to keep the previous chromedriver.log ({type(error).__name__}): "
              f"if this run crashes there will be no earlier session's log to look at.",
              file=sys.stderr)


# ---------- file helpers -----------------------------------------------------

class CredentialsError(RuntimeError):
    """The credentials file exists but does not read as a usable set of credentials.

    **The reason for a dedicated type is the same as `QueueDecodeError`'s: the bare error
    points at the wrong place.** With the wrong encoding the traceback's last line is
    `read_text`, which looks like "the file cannot be read" — when in fact the file is right
    there, just in the wrong encoding; a missing field raises a bare `KeyError` that shows only
    a string, with no hint that it is the credentials file missing a field. And this read has
    **no try** in either variant's `main()`, so it is the run's only exit for this cause of
    death: if the message cannot say "what to do", the cost is someone reasoning backwards from
    a single `read_text` line.

    **This exception's message never contains a credential value, and that is not cosmetic.**
    It carries only the file name, the byte position and `error.reason` (the encoding kind), or
    the missing field names (the field kind) — not the undecodable byte value, not any decoded
    content, and none of `creds`' keys or values. A bare `UnicodeDecodeError` cannot do that:
    its message prints the offending byte value (`can't decode byte 0xff in position 12`), and
    this file is **credentials from top to bottom**. The message goes into the log, and the log
    can reach the chat platform, so this property sits on the outbound boundary.

    For the same reason, the encoding kind deliberately uses `raise ... from None` to drop the
    exception chain — this **differs** from `_queue_decode_error`'s `from error`, and the
    divergence is deliberate: a queue file's content was never secret, so keeping the chain
    buys a fuller diagnosis; the credentials file is secret, and our message already carries
    the position and the reason, i.e. everything a person needs to fix it — the chain would
    only add that one byte value.

    **Only "the file exists but its content is wrong" is wrapped.** `FileNotFoundError` and the
    other `OSError`s (missing / permissions) still propagate as they are: that is a different
    matter, and the callers' comments are written that way too.
    """


def missing_credentials_message(path: Path) -> str:
    """The text printed when the credentials file does not exist. A pure function, one copy
    shared by both variants.

    **It replaces a traceback.** A brand-new clone always lands here on its first batch run,
    and the bare `FileNotFoundError`'s last line is `read_text`, which looks like "the file
    cannot be read" — the reader has to work out alone "what this file is, where the values
    come from, and what the format looks like". Those three things are the entire content of
    the text below.

    The message carries only the **file name**, not the full path: this line goes into
    `webrunner.log`, and the log can reach the chat platform (`/log tail`), where host paths
    must not be sent.
    """
    example = path.name.replace(".md", ".example.md")
    return (
        f"webrunner: {path.name} not found. It holds the login credentials for the image "
        f"service, and the repo deliberately does not ship it. Copy {example} to {path.name} "
        f"and fill in your own account:" + "\n"
        + f"    username: <your username>" + "\n"
        + f"    password: <your password>" + "\n"
        + "(Split on the first colon, so the password may contain colons.) "
          "See docs/setup.md for the detailed steps.")


def read_credentials(path: Path) -> tuple[str, str]:
    """Read the credentials file and return `(username, password)`.

    **The parsing semantics are part of the contract; do not change them**: split on the first
    `:` (so a value may contain colons), the key is `strip().lower()`, the value is `strip()`,
    lines without a colon are skipped, and a repeated key overrides the earlier one.

    "The file exists but does not read as a set of credentials" always becomes
    `CredentialsError` (see that class for why); "the file is missing / permissions" still
    propagates as is.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        # Carry only the position and the reason, not `error.object` nor that byte's value; for
        # why `from None`, see `CredentialsError` (the chained original exception would print
        # the byte value).
        raise CredentialsError(
            f"{path.name} is not UTF-8: it cannot be decoded from byte {error.start} onwards"
            f" ({error.reason}). Re-save this file as UTF-8 and run again."
        ) from None
    creds: dict[str, str] = {}
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        creds[key.strip().lower()] = value.strip()
    # Name only the missing field names — those two names are literals written here, not data
    # read in, so this sentence cannot carry credential content. Nothing that was read
    # (other key names included) is printed.
    missing = [field for field in ("username", "password") if field not in creds]
    if missing:
        raise CredentialsError(
            f"{path.name} is missing the {' and '.join(missing)} field. Make sure every line has"
            f" the form \"field name: value\" (both a `username` line and a `password` line are"
            f" required)."
        )
    return creds["username"], creds["password"]


class QueueDecodeError(RuntimeError):
    """A queue file (`todo_*.md` and their fallbacks) is not UTF-8 and cannot be read.

    **Here this deliberately does the opposite of this module's other loaders: it does not
    fall back to an empty value, it raises.** Those loaders (config files, lock files, progress
    checkpoints) can safely fall back to a default, because downstream "the default" and "the
    value read" can be told apart. Queues cannot — downstream, "empty" is a **legitimate value
    that changes behaviour**:

    - When `read_queues()` sees that a queue is empty, it substitutes the fallback file
      (`prompt.md` / `character1.md` / `character2.md` / `undesired.md`). So "cannot be read"
      quietly becomes "generate from different content", and since fallback mode does not pop,
      the run even ends with **rc=0** saying all is well. The user sees "finished" and gets the
      wrong images.
    - A blank row in `todo_character2.md` itself means "no Character 2 for this pair"; an empty
      `undesired.md` itself means "no negative prompt". Downstream has no way at all to tell
      "really empty" apart from "undecodable".

    And no retry can rescue it — someone has to re-save the file as UTF-8. So the right shape
    is **to fail loudly before Chrome opens**: `run_preflight()` calls `read_queues()` before
    boot, the process dies within a second, and the supervisor's rapid-fail giveup
    (`rapid_fail_giveup_count` = 5 consecutive failures within `rapid_fail_threshold_sec` = 30
    seconds) stops and notifies, so it does not turn into endless respawning.

    A half-write landing right in the middle of a multi-byte character is not hypothetical:
    `write_todo_characters` **deliberately overwrites in place without an atomic write** so the
    editor reloads silently (see that function's docstring for why), and queue files are the
    only class of file in this repo that gives up write atomicity.
    """


def _queue_decode_error(path: Path, error: UnicodeDecodeError) -> QueueDecodeError:
    """Replace the bare `UnicodeDecodeError` with an error that can say "what to do".

    The bare one's traceback ends on `read_text`, which looks like "the file cannot be read";
    in fact the file is right there, just in the wrong encoding. The message carries the file
    name and the byte position so a person has something to go on.
    """
    return QueueDecodeError(
        f"{path.name} is not UTF-8: it cannot be decoded from byte {error.start} onwards"
        f" ({error.reason}). Re-save this file as UTF-8 and run again."
    )


def read_text_safe(path: Path) -> str:
    """The reader for fallback files. **Swallows only "file not found"** — not found means this
    fallback is not configured, and returning an empty string is the right semantics. Every
    other failure (permissions, encoding) propagates; see `QueueDecodeError` for why."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError as error:
        raise _queue_decode_error(path, error) from error


def read_todo_characters(path: Path, *, preserve_blank: bool = False) -> list[str]:
    """One entry per line. Trailing punctuation (including 「，」) within an
    entry is preserved — only newlines split entries. Non-breaking spaces
    (U+00A0) that often sneak in from copy/paste are normalised to regular
    spaces so NovelAI prompts don't carry invisible junk.

    File missing → `[]` (the queue was never created, same as an empty queue). File present
    but not UTF-8 → **raises `QueueDecodeError`, does not return `[]`**; for why that is the
    right direction, see that class's docstring."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except UnicodeDecodeError as error:
        raise _queue_decode_error(path, error) from error
    if raw == "":
        return []
    lines = [line.replace("\xa0", " ").strip()
             for line in raw.splitlines()]
    if preserve_blank:
        # todo_character2.md is positional: an empty row means this image must
        # remove/disable Character 2. Dropping it would shift every later
        # prompt forward and reuse the wrong character.
        return lines
    return [line for line in lines if line]


def write_todo_characters(path: Path, entries: list[str]) -> None:
    """Persist a todo list back to disk, one entry per line.

    It deliberately "overwrites in place" (path.write_text, same inode); **do not** change it
    back to an os.replace atomic write: users often keep todo_prompt.md open in an IDE to watch
    the queue drain live, and os.replace swapping the inode makes PyCharm treat it as "the file
    was deleted and recreated", popping the memory/disk dialog on every pop; overwriting in
    place lets an open file with no unsaved edits reload silently, with no dialog.
    The price is giving up atomicity — but todo files are small and writes take milliseconds,
    so the odds of a hard kill landing exactly in the write window are tiny, and edits coming
    from the bot are protected by .backup/ + reconcile. The key to resuming after an
    interruption is the resume checkpoint (webrunner_progress.json), which stays atomic in
    _run_progress.
    On-disk contract: a non-empty list is joined with "\n" plus one trailing "\n"; an empty
    list is written as 0 bytes."""
    text = "\n".join(entries)
    path.write_text(text + ("\n" if entries else ""), encoding="utf-8")


def reconcile_todo_with_disk(path: Path, remaining: list[str], *,
                             preserve_blank: bool = False) -> list[str]:
    """Re-read `path` before a pop rewrites it. If the on-disk queue diverges
    from our in-memory `remaining` (an external editor or the bot changed it
    mid-run), back up the RAW disk bytes to .backup/ and adopt the disk
    version as the new authority so we never silently clobber the edit.
    Returns the list to treat as current (disk on divergence, else
    `remaining` unchanged)."""
    # Read it with read_todo_characters so both sides of the comparison get the same
    # normalisation (NBSP to space; ordinary queues skip blank lines, Character2 keeps its
    # positional blanks), so layout differences do not cause a false divergence.
    disk = read_todo_characters(path, preserve_blank=preserve_blank)
    if disk == remaining:
        return remaining  # the common "not changed externally" path, a pure no-op.
    # Divergence detected: back up the "raw" bytes on disk (not normalised), adopt the disk
    # version as the new source of authority, and never overwrite the user's / the bot's edit.
    #
    # This goes through read_bytes / write_bytes, **without decoding**. The backup's job is
    # byte-level fidelity; decoding and re-encoding is unnecessary and lossy (`write_text`
    # turns "\n" into os.linesep). More importantly: this used to be `read_text` +
    # `except OSError: raw = ""`, and `UnicodeDecodeError` is a subclass of `ValueError`,
    # **not** `OSError` — when the file is not UTF-8 that line raises outright, which is
    # exactly when a backup is needed most. Adding it to the except tuple would not be right
    # either: that path writes an **empty** backup file and still prints "the original disk
    # content was backed up to X" — claiming it was saved while nothing was. If the backup
    # cannot be saved, say "it was not saved".
    backup_name = ""
    try:
        raw = path.read_bytes() if path.exists() else b""
        # The same naming rule as discord_bot.py's _backup_path_for (timestamp + milliseconds),
        # so the bot's _reconstruct_undo_stack / !undo can pick this backup up. discord_bot must
        # not be imported (the webrunner must not import the bot), so the naming is redone here.
        backup_dir = PROJECT_ROOT / ".backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = time.time()
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(ts))
        ms = f"{int(ts * 1000) % 1000:03d}"
        backup = backup_dir / f"{path.name}.{stamp}.{ms}.bak"
        backup.write_bytes(raw)
        backup_name = backup.name
    except OSError as error:
        # `!r` is kept on purpose: only `OSError` can arrive here, and `str()` would carry the
        # full host path of `.backup/` into the log. `repr()` shows the errno and the reason,
        # not the path.
        print(f"reconcile_todo_with_disk: {path.name} backup failed: {error!r}",
              file=sys.stderr)
    if backup_name:
        print(f"  WARN: {path.name} was modified externally during the run; the disk version "
              f"wins, and the original disk content was backed up to {backup_name} (the edit is "
              f"not overwritten)")
    else:
        print(f"  WARN: {path.name} was modified externally during the run; the disk version "
              f"wins, but **backing up** the original disk content **failed**, so this edit "
              f"cannot be restored with undo")
    return disk


# Windows reserved device names (the whole set listed in Microsoft's "Naming Files, Paths,
# and Namespaces", including `COM0`/`LPT0` and the superscript variants). The comparison is
# made on "the part before the first `.`", case-insensitively — the documentation says
# outright that "these names followed immediately by an extension" are reserved too.
#
# ⚠️ **The behaviour measured on this machine is not what intuition says; read this before
# changing this block** (Windows 11, CPython 3.14): `CON`/`AUX`/`PRN`/`COM1`/`NUL.txt` can
# in fact all be created and written as **directories**; the only one that really breaks is
# `NUL`, and it breaks in the worst way — `Path(box / "NUL").mkdir(parents=True,
# exist_ok=True)` **does not raise** (`exists()` returns True, `is_dir()` returns False,
# because it is the null device), so `generate_loop` carries on, and then writing **every
# image** raises `FileNotFoundError`. So the symptom is not "the batch blows up on the spot"
# but "not a single image of the whole character gets saved", finally ending via
# `consecutive_fail_abort`. Blocking the whole set is deliberate: this is platform- and
# version-dependent behaviour, and it is not worth betting that the next Windows still
# behaves this way for a string nobody would use as a character name.
_RESERVED_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"COM{i}" for i in range(10)] + ["COM¹", "COM²", "COM³"]
    + [f"LPT{i}" for i in range(10)] + ["LPT¹", "LPT²", "LPT³"]
)


def _is_safe_folder_component(name: str) -> bool:
    """True iff `name` can be appended to `OUTPUT_ROOT` as a folder name that is **one level
    and stays inside it**.

    **This is a whitelist assertion, not a character blacklist** — that difference is exactly
    what caused the 2026-09-10 defect: `character_folder_name`'s
    `re.sub(r'[\\\\/:*?"<>|]+', ...)` has no `.`, so a `..` line in the queue became the folder
    name untouched, and `Path("output") / ".."` is **not** normalised by pathlib
    (`parts=('output','..')`); `allocate_output_dir` then returned it as is, and its
    `.resolve()` is `PROJECT_ROOT` — a whole character's images written quietly into the repo
    root, with no exception and no warning. A character blacklist breaks the moment it misses
    one entry, and it breaks in complete silence; `discord_bot._is_unsafe_folder_name`'s
    docstring recorded the same lesson long ago (what it missed was the drive colon), it just
    was never applied to this second copy.

    The rules deliberately **do not overlap** (overlapping rules mask each other, and mutation
    testing mistakes that for "guarded"), so each one below has a test input that trips it
    alone:

    | Rule | Input that trips only this rule |
    |---|---|
    | non-empty | **none** — deliberately redundant, see below for why |
    | no leading/trailing whitespace, no trailing dot | `"a."`, `"a "` |
    | no control characters `< 0x20` | `"a\\tb"` |
    | no `..` | `"a..b"`, `"..foo"` |
    | no drive/root, exactly one level | `"C:x"`, `"a/b"` |
    | first segment is not a reserved device name | `"NUL"`, `"con"` |

    **The control-character rule was not added in passing**: on this machine `mkdir` of
    `"a\\tb"` raises `OSError 123` (ERROR_INVALID_NAME), and `generate_loop`'s
    `out_dir.mkdir(...)` has no try around it, so the exception runs all the way to
    `run_batch`'s outer handler → `critical_error` → rc=1 → the supervisor respawns → the next
    round reads the same queue line and blows up again. A tab slipping into the queue is
    perfectly ordinary (pasted in).

    **What the `..` rule blocks is not path traversal** (traversal is already blocked by the
    "exactly one level" rule), it is **the mirror consistency between the bot and the
    webrunner**: `discord_bot._is_unsafe_folder_name` decides by the substring
    `".." in name`, so as long as we can produce a name like `"a..b"`, the webrunner creates a
    folder the bot cannot open (`/out sample` fails outright for that character). Blocking it
    on the projection side is what keeps the two in agreement. The price is that a character
    name really containing two consecutive dots falls back to the default name — very rare,
    and falling back is safe.

    **The "non-empty" rule is redundant under today's stdlib, but it has its own test; do not
    remove it.** `PureWindowsPath("").parts` on this machine (CPython 3.14.4) is `()`, so the
    "exactly one level" rule below already blocks the empty string. It stays because that is
    **a stdlib implementation detail, and it has really changed before**: before 3.12,
    `PurePath("")` gave `PurePath('.')`. If it ever goes back to `parts == ('.',)` (length 1),
    the empty string passes straight through, and `OUTPUT_ROOT / ""` **is `OUTPUT_ROOT`
    itself** (measured True) — a whole character's images poured into the `output/` root,
    scrambling `allocate_output_dir`'s numbering along the way, and just as silently. The
    empty string **really can reach this point** (`".."` becomes the empty string after the
    trailing-dot normalisation above), so this is not defending against an impossible input.

    ⚠️ **Its guard is
    `test_webrunner_shared.test_the_empty_name_rule_holds_if_pathlib_reverts_to_its_pre_312_shape`,
    which monkeypatches this module's `PureWindowsPath`.** Simply asserting
    `_is_safe_folder_component("") is False` **cannot kill** the "remove this rule" mutant —
    with the mutant applied that assertion is still False, because a different rule blocks
    it. When a rule is masked by another one today, the only way to ask "does it work on its
    own" is to remove the premise that masks it.
    (Before 2026-09-10 this said "the mutant survives, known and on the list" — a SURVIVED
    that hangs on the mutation report for a long time is an invitation for the next person to
    delete the rule, so a test was written instead.)
    """
    if not name:
        return False
    if name != name.strip() or name != name.rstrip("."):
        # Windows **silently** strips trailing dots and spaces when creating a directory
        # (measured: `mkdir("a.")` produces `a`), so keeping such a name makes the name on disk
        # disagree with the one the checkpoint records, and `"a"` and `"a."` collide into
        # the same folder.
        return False
    if any(ord(ch) < 32 for ch in name):
        return False
    if ".." in name:
        return False
    pure = PureWindowsPath(name)
    if pure.drive or pure.root or len(pure.parts) != 1:
        # `PureWindowsPath`, not `Path`: on POSIX `Path` does not recognise `C:` as a drive, so
        # the same test would be green on Linux and red only on Windows. The decision is
        # platform-independent (for exactly the same reason as in `_is_unsafe_folder_name`).
        return False
    return name.split(".", 1)[0].upper() not in _RESERVED_DEVICE_NAMES


def _is_single_path_component(name: str) -> bool:
    """True iff `name` is still **one level** when appended to any directory — it cannot
    change the directory.

    This is deliberately a **weaker** rule than `_is_safe_folder_component`. The two serve
    different purposes:

    | Guard | The question it asks | Used for |
    |---|---|---|
    | `_is_safe_folder_component` | can it be a **folder** name | character folders under `output/` |
    | this one | still a single component after joining? | diagnostic screenshot **file names**, LevelDB `CURRENT` |

    Applying the former to file names would wrongly block: it forbids trailing dots and the
    `..` substring, and `f"ready_{char_name[:30]}"` gets blocked whenever the cut happens to
    fall right after a dot, at the cost of a diagnostic screenshot silently vanishing — a
    guard that cries wolf is a guard that gets switched off.

    `PureWindowsPath`, not `Path`: POSIX `Path` does not treat `\\` as a separator, so using
    `Path` would make the decision follow the platform (for exactly the same reason as in
    `_is_safe_folder_component`).

    ⚠️ **It is also one of the guard names the join guard recognises**
    (`test_bot_helpers._JOIN_GUARDS`). Pulling it out into a named function is not just for
    readability: guarding is recognised by "was this name fed into one of the guards", so
    written as an inline expression, the join site would be judged unguarded.

    ⚠️ **A bare `".."` needs its own rule, because the round-trip below lets it through**
    (added 2026-09-11). Put three measurements side by side and it is clear there was never a
    policy here:

        PureWindowsPath(".").name  == ""    ->  "" != "."   ->  blocked
        PureWindowsPath("..").name == ".."  ->  passes as is ->  allowed
        PureWindowsPath("").name   == ""    ->  blocked by bool(name)

    In other words the answers for `.` and `..` are **both side effects of pathlib's
    normalisation, one right and one wrong by chance**. That asymmetry is not "someone only
    blocked half of it" — the whole decision was outsourced to a predicate, and that predicate
    asks "does it survive normalisation unchanged", not "can it change the directory".
    Measured: `mkdir` and writes on `base / ".."` **both succeed** (landing one level up),
    which is exactly what this function's first sentence claims to block.

    Today none of the three call sites can hand over a bare `".."`: `snap` guards
    `f"debug_{tag}.png"`, whose literal prefix is a structural guarantee; `_leveldb_manifest_ok`
    has a `startswith("MANIFEST-")` gate on the line before. So this is **a latent defect, not
    a live hole**. There are two reasons to fix it: to make the first sentence's contract true,
    and to stop `_JOIN_GUARDS`' books from recording it as "guarded" — that test only asks
    "was the name seen by a guard", not what happens after, so a hole in the guard itself
    shows up green on its books.

    **Only the bare `".."` is taken, deliberately.** `"..."` / `" .."` do collapse back to the
    base when measured, but the write that follows is `FileNotFoundError` — **a loud failure**,
    a different class from `".."`'s silent write one level up; `"a.."` / `"..a"` / `"a..b"`
    stay under the base and can be written, i.e. they are **legitimate file names**. Tightening
    in that direction would block `"debug_ready_a."` on the spot, and that is exactly the input
    `test_is_single_path_component_is_weaker_than_the_folder_guard` uses to prove "this one is
    weaker than `_is_safe_folder_component`" — tightening would merge the three levels of
    strictness that §8.33 ruled must not be merged. Measured over a 61-entry corpus, this fix
    changes exactly **1** entry: the bare `".."`.

    `"."` is not written as an explicit special case, so as not to create a mutant that is
    bound to survive (`name in (".", "..")` changed to `name == ".."` stays all green, because
    `"."` is blocked by a different mechanism). Its premise is instead pinned by a test
    asserting `PureWindowsPath(".").name != "."` — the same approach as the `_PWP("").parts`
    case in `_is_safe_folder_component`.
    """
    if name == "..":
        return False
    return bool(name) and PureWindowsPath(name).name == name


def character_folder_name(prompt: str) -> str:
    """Use the first half-width-comma segment as the human-readable name.

    What it projects is always "a **single-level** folder name under `output/`" — anything
    that does not qualify falls back to the default `"character"` (numbered by
    `allocate_output_dir`). The decision lives in `_is_safe_folder_component`, whose docstring
    has the full reasoning.

    ⚠️ **This function may only be changed in the "stricter" direction.**
    `discord_bot._alert_mentions()`'s docstring states outright that the only reason alert
    messages are not a ping amplifier is that the `re.sub` here incidentally turns `<` `>`
    into underscores (character names are filled in by users via `/todo char1 add`), and it
    warns "the day someone loosens it, or switches to the POSIX character set, this quietly
    turns back into a ping amplifier". So the 2026-09-10 fix **kept the original replacement
    and added a whitelist assertion after it**, rather than swapping the blacklist for a looser
    character set. Read that passage before touching the regex.

    Three consumers: `run_batch` (which actually creates the folder), the event and progress
    records, and the bot's `/gen plan` preview (`discord_bot.py` imports this function). The
    guard can only live here — putting it in `allocate_output_dir` would make the preview and
    the real run diverge, which is exactly the drift the module-boundary rule guards against.
    """
    head = prompt.split(",", 1)[0].strip() or "character"
    name = re.sub(r'[\\/:*?"<>|]+', "_", head)[:120]
    # Normalise trailing dots and whitespace first, then assert. The order cannot be reversed:
    # Windows strips them by itself, so the name after stripping is the real name on disk
    # (`"Mr. Smith."` → `"Mr. Smith"`, identity preserved). Cutting at 120 characters can also
    # happen to leave a trailing dot or space.
    # Side effect: `"."` / `".."` / `"..."` all become the empty string after stripping, so one
    # rule cleans them all up — and all three really are harmful, but **each in a different
    # way** (measured on this machine, CPython 3.14 / Windows 11; all three `resolve()` to a
    # clean answer, the whole difference is in "can it be written to"):
    #
    #   `output/..`   → resolves to `PROJECT_ROOT`, **and can be written to**: images land
    #                   straight in the project root. This is the original escape.
    #   `output/.`    → resolves to `output` itself, **also writable**: images pour into the
    #                   `output/` root, scrambling `allocate_output_dir`'s numbering too.
    #   `output/...`  → also resolves to `output` itself and `is_dir()` even returns True, but
    #                   **not a single byte can be written** (`FileNotFoundError`). So it is
    #                   not "pour into output/", it is every image failing, all the way until
    #                   `consecutive_fail_abort` shuts the whole batch down. Same for `"...."`.
    #
    # ⚠️ This block originally described `.` and `...` as the same behaviour. It is corrected
    # specifically because **this function's docstring has been wrong twice** (see the record
    # above), and each time the cost was the next person reasoning from the wrong description.
    # `is_dir()` returning True while nothing can be written is exactly the shape of "looks
    # already verified" — the criterion must be "can it be written to", not "what does it
    # resolve to".
    name = re.sub(r"[.\s]+\Z", "", name)
    return name if _is_safe_folder_component(name) else "character"


# ---------- keep the operating system from interrupting this process during a batch ----------
#
# Added 2026-09-03, corrected 2026-09-20 (the correction is in the passage below; read them
# together). An unattended batch runs for hours, and this machine **only supports S0 low
# power idle** (Modern Standby; `powercfg /a` shows S1/S2/S3 all unsupported); the system
# event log has 1400 "entering standby" events since 05-26 — 14 a day on average. Without any
# protection, the process gets suspended by PLM (process lifetime management) during standby,
# the batch stops moving, and nothing shows from the outside: the process is still alive, it
# has not crashed, the log has simply stopped.
#
# **Why not just `SetThreadExecutionState`.** That is an S3-era API; Microsoft's documentation
# says outright that it **cannot block** Modern Standby transitions. S0ix machines need a power
# request object (`PowerCreateRequest` + `PowerSetRequest`) with
# `PowerRequestExecutionRequired` — that request type is itself "Modern standby only". So the
# power request is the primary mechanism here, with `SetThreadExecutionState` as the fallback
# (for older machines that still have S3).
#
# **Correction (2026-09-20, measured): the power request protects the "process", not the
# "system".** `PowerRequestExecutionRequired` is defined as "the calling process continues to
# run instead of being suspended or terminated by process lifetime management (PLM)
# mechanisms" — it lets **this process** keep running during Modern Standby; it does **not**
# keep the system from entering Modern Standby. Only on **traditional S3** machines does an
# active `PowerRequestExecutionRequired` also imply `PowerRequestSystemRequired`, i.e. only on
# those machines does it incidentally block system standby. This machine has no S3, so there
# is no such side effect.
#
# Measured (2026-09-20, and while holding the power request): the system log
# `Microsoft-Windows-Kernel-Power` recorded at `04:25:49`
# "[506] The system is entering Modern Standby Reason: Idle Timeout.", and `WEBRunner.log`
# kept generating images after that (05:04, 05:05, ..., 09:32), with no "[507] exiting
# Modern Standby" in between. **The system really did sleep, and the batch really did keep
# running.**
#
# **Same outcome, different description — and that difference sends the next person looking
# in the wrong direction.** For the batch, "kept generating images" is exactly the result we
# want, so it is easy to think this is just wording. It is not: on 2026-09-20 itself, because
# this said the power request also blocked Modern Standby, someone went chasing "was the
# graphics card hang caused by standby" — in the end it was ruled out by the base rate (over
# the last three months, 1426 completed standby intervals covered 34.6% of the whole window,
# and 4 of the 12 datable hangs fell inside standby; 4/12 ≈ 33%, exactly the base rate, no
# correlation). One wrong comment cost a real stretch of debugging time.
#
# **※ When running on battery the system revokes this request.** Microsoft's documentation: on
# a Modern Standby machine on **DC power**, both `system` and `execution required` power
# requests are terminated "5 minutes after the system sleep timeout". So once unplugged, this
# protection does not survive those 5 minutes and the process goes back to being suspendable
# by PLM — and the symptom is exactly the one this whole comment was written to prevent: the
# process is still there, the log has just stopped, nothing shows from outside. To run long
# batches unattended, **keep it plugged in**; there is no fix for this on the code side, so it
# is written here where the next person can find it.
#
# **Deliberately does not request that the display stay on** (no `ES_DISPLAY_REQUIRED` /
# `PowerRequestDisplayRequired`): the batch needs no visible screen, and keeping someone
# else's screen lit all night is rude.
#
# **ctypes `argtypes` / `restype` must be written**, for exactly the same reason as the PID
# liveness probe rule in CLAUDE.md: `PowerCreateRequest` returns a 64-bit HANDLE, the default
# `c_int` truncates it, and **the truncated handle is still non-zero**, so `if not handle`
# does not catch it and the whole feature quietly degrades into "thinks the request succeeded
# but it did not".
_POWER_REQUEST_CONTEXT_VERSION = 0
_POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
_POWER_REQUEST_EXECUTION_REQUIRED = 3      # protects "this process is not suspended by PLM"
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


class StayAwake:
    """Ask the operating system not to interrupt this process during a batch; `release()` or
    the process ending lifts it.

    **Never raises**: this is a bonus, not a requirement for the batch. If it cannot be
    obtained, print one line and carry on — keeping a whole batch of image generation from
    starting over a power-saving setting would be backwards.

    `active` tells what was actually obtained:

    * `"power-request"` — `PowerRequestExecutionRequired`. It protects **this process**: not
      suspended or terminated by PLM during Modern Standby. It does **not** keep the system
      from entering Modern Standby (measured 2026-09-20; for the full evidence and "why this
      difference matters", see the block comment above). Only on traditional S3 machines does
      it also imply `PowerRequestSystemRequired`. Also: on DC power the system revokes it 5
      minutes after the system sleep timeout.
    * `"execution-state"` — the old `SetThreadExecutionState` flag. It only blocks the old S3
      idle sleep; on a Modern Standby machine it neither blocks standby nor protects this
      process.
    * `None` — neither was obtained.

    To the batch the two may **look** the same (it keeps generating images), but what they
    guarantee differs, so those three log lines in `run_batch` print `active`'s literal value
    as well.
    """

    def __init__(self) -> None:
        self.active: str | None = None
        self._handle = None
        self._kernel32 = None

    def acquire(self, reason: str = "axiomatic batch is generating") -> str | None:
        if os.name != "nt":
            return None
        try:
            import ctypes
            import ctypes.wintypes as wt
        except Exception:  # pylint: disable=broad-except
            return None
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._kernel32 = kernel32

            class _Context(ctypes.Structure):
                _fields_ = [("Version", wt.ULONG),
                            ("Flags", wt.ULONG),
                            ("SimpleReasonString", wt.LPWSTR)]

            kernel32.PowerCreateRequest.argtypes = [ctypes.POINTER(_Context)]
            kernel32.PowerCreateRequest.restype = wt.HANDLE   # truncation would break the probe
            kernel32.PowerSetRequest.argtypes = [wt.HANDLE, ctypes.c_int]
            kernel32.PowerSetRequest.restype = wt.BOOL

            context = _Context(_POWER_REQUEST_CONTEXT_VERSION,
                               _POWER_REQUEST_CONTEXT_SIMPLE_STRING, reason)
            handle = kernel32.PowerCreateRequest(ctypes.byref(context))
            # INVALID_HANDLE_VALUE is -1, not 0 — checking only for falsy would miss it.
            if handle and handle != wt.HANDLE(-1).value:
                if kernel32.PowerSetRequest(
                        handle, _POWER_REQUEST_EXECUTION_REQUIRED):
                    self._handle = handle
                    self.active = "power-request"
                    return self.active
                kernel32.CloseHandle(handle)
        except Exception as error:  # pylint: disable=broad-except
            # `!r` is kept on purpose: the `try` holds only ctypes / WinDLL and cannot reach
            # the driver.
            print(f"  [power] could not obtain a power request ({error!r}); falling back to the "
                  f"old API",
                  file=sys.stderr)

        # Fallback: on machines that still have S3 this is enough; on S0ix machines it neither
        # blocks standby nor keeps the process from being suspended by PLM, but holding it does
        # no harm.
        try:
            import ctypes
            import ctypes.wintypes as wt
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.SetThreadExecutionState.argtypes = [wt.DWORD]
            kernel32.SetThreadExecutionState.restype = wt.DWORD
            if kernel32.SetThreadExecutionState(
                    _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED):
                self._kernel32 = kernel32
                self.active = "execution-state"
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        return self.active

    def release(self) -> None:
        """Idempotent, never raises (it is called from `finally` and may already be released)."""
        kernel32, handle, active = self._kernel32, self._handle, self.active
        self._handle, self.active = None, None
        if kernel32 is None:
            return
        try:
            if handle is not None:
                kernel32.PowerClearRequest(handle,
                                           _POWER_REQUEST_EXECUTION_REQUIRED)
                kernel32.CloseHandle(handle)
            elif active == "execution-state":
                kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass


# ---------- timing / anti-bot pacing ----------------------------------------

def human_pause(lo: float = 0.4, hi: float = 1.2) -> None:
    time.sleep(random.uniform(lo, hi))


def human_type(element, text: str, slow: bool = True) -> None:
    if not slow:
        element.send_keys(text)
        return
    for ch in text:
        element.send_keys(ch)
        time.sleep(random.uniform(0.02, 0.07))


# ---------- retry harness ----------------------------------------------------

def with_retry(label: str, func, max_attempts: int = 3,
               sleep_range: tuple[float, float] = (2.0, 4.0)) -> bool:
    """Run `func` (a zero-arg callable returning truthy on success); retry on
    falsey return or exception, sleeping a random interval between attempts."""
    for attempt in range(1, max_attempts + 1):
        try:
            result = func()
        except BrowserGoneError:
            raise
        except Exception as error:  # pylint: disable=broad-except
            # Retrying once the window / session has vanished is pure waste (every setup step
            # would burn its own 3 attempts), and it buries the real cause of death under a
            # string of "gave up" lines. The original exception is escalated here too —
            # variant helpers such as `port.click()` swallow it with a broad except and then
            # re-raise it from the JS fallback, which is how a raw WebDriverException leaks out.
            if is_browser_gone_error(error):
                raise BrowserGoneError(
                    f"browser session gone during {label} — "
                    f"{_long_error(error)}") from error
            # `_short_error`: this line is a repeated line printed for **every** DOM step on
            # every retry, so it uses the 180-character version. With `!r`, a selenium exception
            # (whose `args` is empty) prints only an empty pair of parentheses — while the
            # `BrowserGoneError` two lines up in the same handler already uses `_long_error`:
            # the same exception object, two formats, one with the message and one without.
            print(f"  [{label}] attempt {attempt}/{max_attempts} raised: "
                  f"{_short_error(error)}")
            result = False
        if result:
            if attempt > 1:
                print(f"  [{label}] succeeded on attempt {attempt}")
            return True
        if attempt < max_attempts:
            delay = random.uniform(*sleep_range)
            print(f"  [{label}] attempt {attempt}/{max_attempts} failed; "
                  f"retrying in {delay:.1f}s")
            time.sleep(delay)
    print(f"  [{label}] gave up after {max_attempts} attempts")
    return False


# ---------- output folder allocation ----------------------------------------

def _folder_belongs_to_batch(folder: Path, batch_start: float) -> bool:
    """A folder is part of the current batch when at least one file inside it
    was written after `batch_start`. Empty folders count as belonging to the
    current batch so we re-use them without numbering."""
    if not folder.exists():
        return False
    try:
        mtimes = [
            p.stat().st_mtime for p in folder.iterdir() if p.is_file()
        ]
    except OSError:
        return False
    if not mtimes:
        return True
    return max(mtimes) >= batch_start


def allocate_output_dir(base_name: str, batch_start: float) -> Path:
    """Return the output folder for this batch. If `<base_name>/` already has
    files from an earlier batch, walk through `<base_name>_2`, `_3`, … until
    a free or current-batch folder is found."""
    base = OUTPUT_ROOT / base_name
    if not base.exists() or _folder_belongs_to_batch(base, batch_start):
        return base
    for i in range(2, 1000):
        candidate = OUTPUT_ROOT / f"{base_name}_{i}"
        if not candidate.exists() or _folder_belongs_to_batch(candidate, batch_start):
            return candidate
    return base  # 1000 conflicts — give up and overwrite


# ---------- DOM leaf helpers (P6 C3 — port-based; signature `fn(port, ...)`) ----
# Lifted verbatim (logic-identical) from the two webrunner variants. All DOM
# access goes through the injected `port`; no selenium / je_web_runner import
# here (transport-error tuple + ancestor walk are reached via `port`).


def dump_textareas_diag(port) -> list[dict]:
    """Snapshot the key attributes of every textarea / `contenteditable=true` on the page.
    Returns a list of dicts; any exception is swallowed and an empty list returned."""
    try:
        result = port.execute_script(_DOM_DIAG_JS)
        return result if isinstance(result, list) else []
    except Exception as error:  # pylint: disable=broad-except
        # `_short_error`: the callers are `_fill_via_native_setter`'s mismatch branch and
        # `find_undesired_textarea`'s not-found branch, both failure paths that **every fill**
        # can take, so this counts as a repeated line.
        print(f"dump_textareas_diag failed: {_short_error(error)}",
              file=sys.stderr)
        return []


def check_dom_request(port) -> None:
    """Called at iteration boundaries; when DOM_REQUEST_FILE is present, dump a textarea diag
    and `emit_event('dom_result', ...)` for the bot watcher to pull back to the channel; the
    request file is deleted once handled, success or failure, so it does not trigger again."""
    if not DOM_REQUEST_FILE.exists():
        return
    try:
        # The content is not parsed yet; there is currently only one mode, "dump everything".
        # A cmd field may be added later.
        DOM_REQUEST_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # `UnicodeDecodeError` is a subclass of `ValueError`, not `OSError`, so `OSError`
        # alone cannot catch it. The content is not even used here, and letting a request file
        # with broken encoding blow up the whole batch makes no sense — when the cmd field is
        # actually parsed some day, this except must change to "report that this request is
        # broken" instead of carrying on as if it were a valid request.
        pass
    try:
        diag = dump_textareas_diag(port)
        emit_event("dom_result", count=len(diag), data=diag)
        print(f"check_dom_request: emitted dom_result with {len(diag)} entries")
    except Exception as error:  # pylint: disable=broad-except
        emit_event("dom_result", error=str(error))
        # `_long_error`: one `dom_request.json` reaches this at most once (the block below
        # deletes the request file whether or not it succeeded), so it is a one-off diagnostic,
        # not a repeated line.
        print(f"check_dom_request failed: {_long_error(error)}",
              file=sys.stderr)
    try:
        DOM_REQUEST_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def snap(port, tag: str) -> None:
    if not _DEBUG_SCREENSHOTS:
        return
    name = f"debug_{tag}.png"
    if not _is_single_path_component(name):
        # `tag` may only contribute **a file-name fragment**, never change the directory.
        # Today the tags of all 26 callers are literals / integers / sanitised character
        # names, so this blocks no legitimate diagnostic screenshot; what it blocks is "some
        # future caller splicing an external string into tag" — `request_id` once played that
        # role, and measured, `../../../evil` writes outside the repo (the parent directory
        # exists, so the write **succeeds**, it does not error). For why this uses
        # `_is_single_path_component` rather than the stricter `_is_safe_folder_component`,
        # see the former's docstring.
        print(f"[{tag}] screenshot skipped: tag is not a single filename")
        return
    shot = PROJECT_ROOT / name
    try:
        port.save_screenshot(str(shot))
        print(f"[{tag}] screenshot -> {shot}")
    except Exception as error:  # pylint: disable=broad-except
        # `_short_error`: `snap()` hangs off twenty-odd error branches, including
        # `generate_loop`'s per-image failure path — once the browser dies, this line gets
        # printed along with every single failure.
        print(f"[{tag}] screenshot failed: {_short_error(error)}")


class BrowserGoneError(RuntimeError):
    """The browser context is gone — not "slow", but "does not exist".

    When a hot-path helper's chromedriver call fails and the failure is **permanent** (the
    window was closed, the target destroyed, the session deleted, chromedriver unreachable),
    this exception is raised instead of the usual None / False. Reason: every later call
    would fail the same way, and the caller's poll / retry budget would all be burned for
    nothing.

    It deliberately inherits `RuntimeError`: it lands in `run_batch`'s outer handler, on
    exactly the same path as `_abort_if_chrome_crashed`'s raise — emit `critical_error`, exit
    non-zero, the supervisor respawns Chrome (the resume checkpoint picks that character back
    up). **Do not** add it to any variant's `TRANSPORT_ERRORS` tuple; the whole point of the
    design is that it must not be absorbed by those `except port.TRANSPORT_ERRORS` branches.
    """


# The two lengths used when squashing an exception into one line. There are two because the
# costs of the two uses are completely different:
#   `_short_error` (180) is for log lines that **repeat** — printed once per turn of a poll
#     loop, where a long message is spam.
#   `_long_error` (400) is for **one-off** places: a terminal raise (that sentence becomes the
#     message of the `critical_error` event), and the diagnostic for a click that will not go
#     through. In both, the key information sits at the **tail** of the message —
#     `MaxRetryError`'s `(Caused by NewConnectionError(... [WinError 10061] ...))` and
#     `element click intercepted`'s `Other element would receive the click: <div class=…>` —
#     and 180 characters happens to cut off both.
_SHORT_ERROR_LIMIT = 180
_LONG_ERROR_LIMIT = 400


# ---------------------------------------------------------------------------
# Why 34 sites still deliberately keep `{error!r}`
#
# On 2026-09-12 every place in these three files (the two variants + this module) that
# "inserts an exception straight into a string" was inventoried: **46 sites**, of which only
# 12 were switched to the three formatters above. The remaining 34 are not missed changes;
# the criterion is a single sentence —
#
#     **Can this `try` block raise a selenium exception?**
#
# If it can, it must change: `WebDriverException.__init__` calls `super().__init__()` **with
# no arguments**, so `args` is empty and `repr()` prints only an empty pair of parentheses —
# the message is 100% lost (measured on a `SessionNotCreatedException` really thrown back
# from the driver: `str()` 752 characters, `repr()`'s message 0 characters).
#
# If it cannot, `!r` is actually the **better** one, for two reasons:
#
# 1. **It does not leak host paths.** Most of this family is file I/O: `repr(OSError)` is
#    `FileNotFoundError(2, 'No such file or directory')` — **no path in sight**;
#    `str(OSError)` is `[Errno 2] ...: 'D:\Work\...'`, which writes the full host path into
#    `webrunner.log`, and `/log tail` sends that file into the chat platform (Secrecy Layer 1).
# 2. **These exceptions' `args` are non-empty to begin with** (`OSError` / `psutil` /
#    `ctypes` / `ImportError` / `json.JSONDecodeError`), `repr()` can read the message, and
#    changing them would just be noise.
#
# ⚠️ **The criterion is "can the try block reach the driver", not "what type the except
# names".** 20 of the kept sites have handlers written as `except Exception`, but their try
# holds only `shutil.copy2` / `os.walk` / `psutil` / `ctypes` / `emit_event` — a broad
# `except` does not mean it can receive a selenium exception. Conversely, widening an
# `except OSError` to `except Exception`, or adding a line that touches `port` inside an
# existing try, would push that site over the line, so the guard counts "the handler's
# exception type" in its comparison key too.
#
# Guard: `test_selenium_facade.py`'s `_EXCEPTION_REPR_EXEMPT` (reconciled both ways, every
# entry must give a reason). A new site is **red** by default: either switch it to a
# formatter, or register it together with its reason.
# ---------------------------------------------------------------------------


def _one_line_error(error, limit: int) -> str:
    """Squash an exception into one line `Type: message`, cut to `limit` characters.

    chromedriver appends roughly 15 lines of `Stacktrace:` (C++ symbol addresses) to every
    WebDriverException. Printing that block on every poll against a dead session is where
    "one fault turns into pages of noise" comes from, and it has no diagnostic value at all.
    The full details still travel through the `raise ... from error` exception chain, and
    `run_batch` formats them into the traceback field of the `critical_error` event.
    """
    text = str(error).split("Stacktrace:")[0]
    line = " ".join(text.split())
    return f"{type(error).__name__}: {line[:limit]}"


def _short_error(error) -> str:
    """Use this for repeated log lines (see the note above `_SHORT_ERROR_LIMIT`)."""
    return _one_line_error(error, _SHORT_ERROR_LIMIT)


def _long_error(error) -> str:
    """Use this for one-off places: terminal raises, and the diagnostic for a click that will
    not go through.

    **Why not share the 180 version.** In both uses the key information sits at the tail of
    the message:

    * `MaxRetryError`'s `(Caused by NewConnectionError(…: [WinError 10061] No connection
      could be made because the target machine actively refused it.))` — "connection refused"
      is the only evidence that directly answers "has chromedriver.exe already exited", and
      this sentence becomes the `critical_error` event's message untouched (it is
      `BrowserGoneError`'s message).
    * `element click intercepted`'s `Other element would receive the click: <div class=…>` —
      the only evidence that directly answers "is something covering the close button", and
      exactly the diagnostic that moving the click from JS to the driver bought us.

    This path runs at most twice per quota cycle and a terminal raise at most once per round,
    so being a bit longer does not flood the log.
    """
    return _one_line_error(error, _LONG_ERROR_LIMIT)


def full_error_detail(error) -> str:
    """`Type: full message` — the forensic format that **neither truncates nor cuts
    `Stacktrace:`**.

    ⚠️ **Never switch back to `{error!r}`.** selenium's `WebDriverException` stores the
    message in `self.msg` and **`args` is empty**, so `repr()` leaves only an empty pair of
    parentheses. Measured (selenium 4.48.0, 2026-09-12):

        e = SessionNotCreatedException(
            'session not created: This version of ChromeDriver only supports '
            'Chrome version 145\\nCurrent browser version is 152.0.7977.84 ...')
        e.args   -> ()
        repr(e)  -> SessionNotCreatedException()      # the message is 100% lost
        str(e)   -> 351 characters, including both version numbers and Chrome's real path

    **Two numbers; do not mix them up.** The 351 above is an exception **constructed by
    hand** (it is the test corpus). The one really thrown back from the driver is **752
    characters** — selenium appends `Stacktrace:` and a block of unsymbolised backtrace
    addresses after the message (measured 2026-09-12, chromedriver 145 against Chrome 152).
    Both are right, they measure different objects; say which one when quoting.

    This is not hypothetical: for the 2026-08-25 11:38:50 spawn failure the log held only
    `Chrome spawn attempt 1/3 failed: SessionNotCreatedException()` and "nothing else", so
    someone went digging through a `chromedriver.log` that had not even been created yet.
    **The message was never empty; our own format threw it away.**

    ⚠️ **This defect cannot be caught with an ordinary `Exception('boom')`**: such an
    exception's `args` is non-empty, `repr()` includes the message, and both the old and the
    new format pass. To pin it down, the corpus must be a real selenium exception (`args`
    empty, `msg` set).

    **Keep the type name.** For an exception with non-empty `args`, `str()` has only the
    message and the class cannot be recognised, and the difference between
    `SessionNotCreatedException` and `TimeoutException` is itself the first layer of
    diagnosis.

    **Why this is not routed through `_one_line_error` (that 400-character cap would bite).**
    `_short_error` / `_long_error` truncate and also cut off the C++ `Stacktrace:` chromedriver
    appends, because they serve log lines that **repeat** and terminal raises whose message
    **becomes the `critical_error` event message**. This one is neither: it only appears when
    a spawn fails, a few lines per boot at most, written only to stderr, and the useful words
    (both version numbers, Chrome's real path) come **before** the stack dump, so that dump is
    an ignorable tail of bounded length. Conversely, that 400-character cap is now **just
    enough** rather than generous: `_one_line_error` first cuts everything after
    `Stacktrace:` and then squashes newlines into spaces, so the real 752 characters go in and
    **350** come out, only **50** characters short of 400. One more sentence and it gets cut,
    and the symptom of being cut is exactly the one this function exists to fix: diagnostics
    quietly going missing.
    **Do not helpfully "unify" them** — this warning by itself guards nothing (measured:
    changing this function to `return _long_error(error)` left all 41 tests of the whole
    `test_selenium_facade.py` green as of 2026-09-12), so it comes paired with
    `test_the_forensic_format_keeps_what_the_one_line_helpers_throw_away`, which really bites.

    (This does not conflict with the MEMORY note "`repr(OSError)` hides the path, `str`
    exposes it": that note is about **strings sent into the chat platform**, while this is a
    diagnostic written to stderr / the log, and `CLAUDE.md` Layer 1 says outright "full
    details go to stderr / the log". The sending side is still guarded by the bot's
    `_owner_detail`.)
    """
    return f"{type(error).__name__}: {error}"


def is_browser_gone_error(error) -> bool:
    """Whether `error` means "the window / session is permanently gone"."""
    names = {cls.__name__ for cls in type(error).__mro__}
    if names & _SESSION_GONE_EXC_NAMES:
        return True
    text = str(error).lower()
    return any(marker in text for marker in _SESSION_GONE_MESSAGE_MARKERS)


def _note_transport_error(where: str, error) -> None:
    """Common handling for hot-path `except port.TRANSPORT_ERRORS`: log it, escalate when
    needed.

    A transient hiccup → print one line and return, and the caller keeps its original
    "return a sentinel and poll again" behaviour. The session is dead → raise
    `BrowserGoneError`, so this round is shut down at once instead of polling a dead browser
    until the whole retry budget is spent.
    """
    if is_browser_gone_error(error):
        # A terminal raise: this sentence becomes the `critical_error` event's message
        # untouched, so it uses the longer-kept version (`_long_error`'s docstring records
        # why — the key `(Caused by …[WinError 10061]…)` is at the tail of the message).
        raise BrowserGoneError(
            f"browser session gone during {where} — {_long_error(error)}"
        ) from error
    print(f"  [warn] {where} transport error: {_short_error(error)}")


def _browser_gone_reason(port) -> str | None:
    """The cheapest possible JS round-trip; returns a short reason if the browser is gone,
    otherwise None.

    A "transient failure" also returns None — the call sites that reach this already have
    their own failure counters, and that path should not be taken over by this probe.
    """
    try:
        alive = port.execute_script(_ALIVE_PROBE_JS)
    except Exception as error:  # pylint: disable=broad-except
        if is_browser_gone_error(error):
            return _short_error(error)
        return None
    if alive == _ALIVE_PROBE_TOKEN:
        return None
    # je variant: the wrapper swallows exceptions and always returns None. This path cannot
    # tell "the window was closed" from "temporarily stuck", but every call site comes after
    # "a full round of consecutive failures already" — at that point respawning the browser
    # is the right response.
    return f"alive probe returned {alive!r}"


def _probe_browser_alive(port) -> str | None:
    """An ultra-light liveness probe for the quota wait loop. Returns None when alive, and a
    short reason when dead.

    **Never raises, and never attempts any remedy.** Both are deliberate:

    - It runs inside the quota wait loop, and any exception thrown upwards would replace the
      otherwise clean "wait it out, then retry" path; so even `_browser_gone_reason` itself
      breaking must be swallowed (swallowed into a reason, not into silence — a broken probe
      has to be visible).
    - On detecting death it also **only records, and does not restart on the spot**. A
      mid-run restart has to log in again and refill the fields, and that is exactly the path
      of this project's most expensive failure ever (fields only partly refilled, quietly
      burning 10 images / two hours with the wrong prompt). Triggering it during a wait is
      more risk than benefit; leave it to the existing recovery flow at its usual time.

    Its only output is **time**: it shrinks the moment of death from "up to an hour late" to
    within 30 seconds. A full round of the wait loop (3600 seconds by default) issues no
    command at all to the driver, so if the browser dies during the wait, it is only found at
    the first command after the wait. Measured (`WEBRunner.log`, 2026-09-07 at 03:02 and
    06:18) it looks like this:

        ConnectionRefusedError: [WinError 10061] No connection could be made because the
            target machine actively refused it.
        supervisor: webrunner exited rc=1 after 283379s (attempt 1); restarting

    **WinError 10061 is "connection refused" = nothing is listening on that port =
    chromedriver.exe itself has already exited**, not a connection timeout and not a stuck
    session. Both timestamps fall exactly on the first command after a 60-minute wait ended,
    so the real moment of death within that hour is unknowable — any comparison with other
    activity is guesswork. (Incidentally ruling out a plausible explanation: Selenium 4's
    "kill the session after 300 idle seconds" is Grid / selenium-server's
    `--session-timeout`; this project starts `ChromeService` directly on the local machine
    with no Grid, so it does not apply.)

    **2026-09-10: the cause of death was mostly found, and it is not on the browser side — it
    was our own test tooling.** The four deaths (03:02 / 06:17 / 11:44 / 17:10) match four
    incidents: `_browser_killguard.py`'s docstring records **three** of them (it was written
    at 17:06:41, so it could not record the fourth). That one was at 17:07, when the
    `mutate_killguard.py` probe, which verifies the defence itself, really ran
    `taskkill /IM chrome.exe` under the mutant "remove both taskkill checks together".
    **To investigate this family, grep three files together**: the reason the fourth was
    missed is the very thing this conclusion says — the answer is spread across several files
    that do not reference one another.

    The evidence for death #1 is hard: the supervisor's `after 283379s` = 78.72 hours, and
    incident #1 reads "destroyed a batch that had already been running for 78.7 hours" — the
    same batch, the same minute.

    **The last death was at 17:07 (caused by the defence's own mutation probe), with zero
    deaths since.** Do not write it as "zero deaths after the defence landed": counting from
    17:06, the measurement is 52 `quota_blocked` / 50 `quota_resumed` / **1**
    `critical_error` (the 17:10:44 one); the 52/50/**0** set of numbers counts from
    **17:11**, and does not hold if attached to 17:06.

    The two ways of dying have different signatures; do not merge them in statistics:
    03:02 / 06:18 / 11:44 are `ConnectionRefusedError` (chromedriver.exe itself vanished);
    17:07 / 17:10 are `InvalidSessionIdException` (chromedriver.exe still alive, every
    chrome.exe gone = the signature of `taskkill /IM chrome.exe`).

    ⚠️ **This also corrects a framing error: "they all died while waiting for quota" is a
    detection artefact, not a mechanism.** All four deaths are timestamped 60.4–60.6 minutes
    after `quota_blocked`, i.e. the first command after the wait ended — that hour is the
    **only** window that does not touch the driver, so any death inside it is only seen at
    that moment. The control group is in the same log: another death appears 4.6 minutes
    after `character_start`. So stop looking for causes starting from "what during the wait
    would kill the browser"; what that correlation measures is **when we go and look**.

    **And there is direct evidence for this, not only the control group.** The fourth death
    is the only one where this function was already live, and `webrunner.log` still holds the
    line it caught (the only such line in the whole file):
    `[09-07 17:07:44] [quota] …the browser died while waiting for quota (waited 3420s this
    round…)` — 3420 seconds = 57 minutes, **three minutes earlier** than the same event's
    `critical_error` (17:10:44). Put the two timestamps of the same death side by side, and
    "60.4 minutes" is proven to be reporting delay, not a mechanism.
    """
    try:
        return _browser_gone_reason(port)
    except Exception as error:  # pylint: disable=broad-except
        return f"alive probe itself failed: {_short_error(error)}"


def _abort_if_browser_gone(port, where: str) -> None:
    """Raise `BrowserGoneError` (→ the supervisor respawns) if the window / session is gone.

    It and `_abort_if_chrome_crashed` cover **complementary** ways of dying: that one catches
    "the session is still alive but the page became a crash interstitial" (the renderer
    crashed), this one catches "the session itself is gone" — in the latter even
    `port.current_url()` raises, so crash-page detection only ever sees "unreadable, treat as
    not crashed" and would never shut it down.
    """
    reason = _browser_gone_reason(port)
    if reason is not None:
        raise BrowserGoneError(
            f"browser session gone during {where} — {reason}; aborting so "
            f"supervisor respawns Chrome."
        )


class GenerationBlockedError(RuntimeError):
    """The site blocks generation, and **retrying will get nowhere** (quota used up, plan
    expired…).

    The difference from `BrowserGoneError` is in how it wraps up, not in severity: a gone
    browser needs a **respawn** (a new Chrome fixes it), this one needs to **stop and wait for
    a human** (a respawn would only see the same dialog, rerunning login + setup every round,
    which is the "it never stops by itself" users saw).

    `run_batch` catches it specifically, emits a `generation_blocked` event and returns
    `RC_GENERATION_BLOCKED`; both supervisors treat that rc as "do not respawn". The queue and
    the resume checkpoint are left untouched — once the user has dealt with it, `/run` picks
    up right where it left off.
    """


def get_generation_block(port) -> dict | None:
    """Tier 1: is there a dialog or toast on screen saying "pay up / deal with your account".

    Returns `{"text": ..., "pattern": ...}` or None. Read-only, never raises — transport
    exceptions are routed by `_note_transport_error` (a gone session is escalated to
    `BrowserGoneError`, which is a different wrap-up path).
    """
    try:
        return port.execute_script(_GENERATION_BLOCK_JS)
    except port.TRANSPORT_ERRORS as error:
        _note_transport_error("generation-block check", error)
        return None


def has_blocking_dialog(port) -> str | None:
    """Tier 2: is a visible modal dialog blocking the screen (**text is not looked at**).

    Returns the dialog text (cut to 400 characters; beyond that the full length is appended at
    the tail) or None.

    **There are three consumers, not the one the docstring used to name**, and one of them
    treats None as a "yes" answer, so a miss here is not a missed detection but a silently
    wrong result:

    1. `dismiss_blocking_dialog`'s progress criterion — **`None` = "closed cleanly"**
       (`stop_reason = "closed"` → `return True`).
    2. `generate_loop`'s Tier 2 decision at the `consecutive_fail_abort` threshold.
    3. `generate_loop`'s final line of defence (`saved == 0` while a modal is still on
       screen).

    So this question must **err towards answering "yes"**: a false positive only drops 1 into
    a full-page reload (safe), and makes 2/3 stop once more on a character that was already
    dead; a miss makes 1 report success against a dialog it never even touched. That is how
    the length cap came to be removed; the reasoning is written above `_BLOCKING_DIALOG_JS`.
    """
    try:
        return port.execute_script(_BLOCKING_DIALOG_JS)
    except port.TRANSPORT_ERRORS as error:
        _note_transport_error("blocking-dialog check", error)
        return None


def describe_dialog_controls(port) -> str:
    """Turn the list of controls on a blocking dialog into text for the log. Purely
    read-only, clicks nothing.

    Only called on the close-failed path (at most once per quota exhaustion), so its cost does
    not matter; the happy path never runs it. An empty string means it could not be obtained
    (the dialog happened to vanish, or JS could not get through).
    """
    try:
        info = port.execute_script(_DIALOG_CONTROLS_DIAG_JS)
    except port.TRANSPORT_ERRORS as error:
        _note_transport_error("dialog control inventory", error)
        return ""
    if not isinstance(info, dict):
        return ""
    rows = info.get("controls") or []
    head = (f"dialog {info.get('w')}x{info.get('h')}, {info.get('total')} clickable "
            f"candidates (sorted by distance from the top-right corner, first {len(rows)} "
            f"listed):")
    lines = [head]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"    #{i:<2} <{r.get('tag')}> role={r.get('role')!r} "
            f"aria={r.get('aria')!r} title={r.get('title')!r} "
            f"text={r.get('text')!r} icon={r.get('icon')} "
            f"@({r.get('dx')},{r.get('dy')}) {r.get('w')}x{r.get('h')} "
            f"cursor={r.get('cursor')} class={r.get('cls')!r}")
    return "\n".join(lines)


def _click_dismiss_target(port, element) -> str:
    """Press the one `_DISMISS_DIALOG_JS` picked. Returns `'real'` / `'synthetic'` / `''`
    (neither kind of press worked).

    **Why a real driver-level click comes first.** The site stacks two layers of dialog, and
    the top-right close buttons of both layers have exactly the same class, yet JS's
    `el.click()` only closes the first layer (measured: pressing layer 1 really changed the
    screen, pressing layer 2 changed not a single character, consistently across four quota
    cycles). Both mechanical differences can only be made up by a real click:

    * `el.click()` **dispatches directly to that element** and only bubbles up; a real click
      lands on a coordinate, and `e.target` is "the topmost element at that coordinate". When
      something covers it, only a real click reaches that handler.
    * `el.click()` dispatches only the `click` event; a real click goes through the whole
      pointerdown / mousedown / mouseup / click sequence. Hanging the close behaviour on
      `onPointerDown` / `onMouseDown` is a common pattern in modal components, and that kind
      only ever receives a real click.

    **Goes through `click_native`, not `port.click`**: the latter itself **quietly** falls back
    to an `execute_script` synthetic click on failure, so "the real click succeeded" and "the
    real click failed and it secretly switched to synthetic" look exactly the same from
    outside — which is exactly the "the log reports the attempt, not the result" this project
    has recorded four times. Telling those two apart is precisely what is needed here: a real
    click blocked by `ElementClickIntercepted` is direct evidence that "something is covering
    it", and it names that element too. So the fallback is walked here, step by step, each
    step speaking up.

    `getattr` + `callable` is for slimmed-down ports (the tests' fake ports, other
    verification scripts): without that method it goes straight to the synthetic click,
    behaviour falls back to the old version, and nothing blows up.
    """
    if element is None:
        # JS picked something but handed over no element — the only possibility is that the
        # port did not unwrap the WebElement.
        print("  [quota] the dismiss action picked a control but could not get the element; "
              "sending Escape instead",
              file=sys.stderr)
        return ""
    native = getattr(port, "click_native", None)
    if callable(native):
        try:
            native(element)
            return "real"
        except Exception as error:  # pylint: disable=broad-except
            if is_browser_gone_error(error):
                raise BrowserGoneError(
                    "browser gone while dismissing a dialog: "
                    + _long_error(error)) from error
            # This line is the diagnostic this change bought: `element click intercepted`
            # also names the element covering it.
            print(f"  [quota] the real driver click would not go through ({_long_error(error)}); "
                  "falling back to a synthetic click", file=sys.stderr)
    try:
        port.execute_script("arguments[0].click();", element)
        return "synthetic"
    except Exception as error:  # pylint: disable=broad-except
        if is_browser_gone_error(error):
            raise BrowserGoneError(
                "browser gone while dismissing a dialog: "
                + _long_error(error)) from error
        print(f"  [quota] the synthetic click would not go through either ({_long_error(error)})",
              file=sys.stderr)
    return ""


def dismiss_blocking_dialog(port, max_rounds: int = 4) -> bool:
    """Close the blocking modal dialogs, **until there are none left**. Returns True when all
    are closed cleanly (or there were none to begin with).

    Each round: JS **picks** one that is safe to press (text → aria-label → text-less icon
    button in the top-right corner) and hands the element back → a real driver click → if
    that does not work, a synthetic click → only if neither works (or nothing was picked at
    all) is Escape sent → **the result decides**: ask once more whether a modal is still
    there ("pressed something" is not "closed it"). Only when nothing can be closed does the
    caller fall back to a full-page reload.

    Never clicks any control with payment semantics. **The decision point is "JS never
    returns it"**, not "it is never clicked" — the pressing has moved into Python, so a
    counter-example test pinned on the click event would verify nothing. For the reasoning
    and the details of the ladder, see the notes on `_DISMISS_DIALOG_JS` and
    `_click_dismiss_target`.

    **Why a loop rather than a single shot (2026-09-07; this is the most important passage
    of this function).** After rule 3 went live, five quota cycles in a row printed "could
    not close it", which looked like "found the right one but it will not press". What
    overturned that conclusion was not any message, but the **dialog size** printed by
    `describe_dialog_controls` — which changed the moment rule 3 went live:

        before 11:44  856x917 / 11 candidates (Subscribe, Pay As You Go, Get Started) × 65
        after 11:44   420x322 /  5 candidates (Unsubscribe, Update Payment Details)   ×  5

    And that list was captured **after the dismiss action had run**. In other words the first
    paywall layer really was closed, exposing a **second** (account management) dialog behind
    it, and a single-shot dismiss that only asked "is there still a modal" reported failure
    and degraded into a full-page reload. So what was missing was "close it once more", not a
    different way of pressing.

    **The second dialog does not respond to synthetic clicks; that is why a driver click is
    needed on top of "until there are none left".** Every one of the four quota cycles after
    the loop went live looked the same: pressing layer 1 @(805,21) really changed the screen,
    pressing layer 2 @(369,21) changed **not a single character**, the early-stop criterion
    gave up on the spot, and it still fell back to a full-page reload. The close buttons of
    both layers have exactly the same class, so the plausible explanations for the difference
    lie **above the element** (something covering it) or in the **kind of event** (the
    handler hangs on pointerdown/mousedown), and both can only be made up by a real click.

    Along the way "is the second layer an account problem" was ruled out: the raw text
    returned by `has_blocking_dialog` was "You are subscribed to the Opus tier! Your
    subscription renews around 2026/09/18", and after that quota refilled every hour and
    images were generated as usual (8.0 images/hour, consistent with the historical baseline
    of 7.85). So it is just an account-management panel hidden behind the paywall, and the
    right handling is to close it, not to call a human.

    **The progress criterion is the dialog text, not "was it pressed".** Every round compares
    `has_blocking_dialog`'s return: the text changed = that press worked and there is another
    layer to keep closing; the text is identical = that press changed nothing, and pressing
    ten more times would be the same, so give up and let the caller reload. This is the same
    "the result decides" rule taken one step further — without it, a button that will not
    press would eat `max_rounds` rounds for nothing.

    **Escape is sent once per round, and only once.** Against the site's two modal layers it
    was measured **ineffective 218/218 times** (13 days, real key presses + synthetic events,
    all three dispatch targets tried), but it is a generic fallback rather than one specific
    to this site: `verify_quota_dialog.py` scenario 12 (a focus-trap dialog whose keydown hangs
    only on the modal element) proves it really closes other shapes. Removing it would drop
    that scenario back to a full-page reload; sending it repeatedly is pure side effect
    (Escape means other things on other screens).

    **Do not compute the success rate from log messages.** `WEBRunner.log` has 153
    "dismissed" against 65 "could not dismiss", which looks like a 70% success rate — all 153
    are false, coming from this function's old defect of "printing dismissed unconditionally
    before verifying" (the comment below records it). What reveals the truth is a
    **structural** contradiction: a reload only happens when `not dismissed`, and every day's
    reload count exactly equals that day's attempt count, including the "successful" days.

    When it cannot close, it captures the list of controls on the dialog
    (`describe_dialog_controls`). This is not leftover debugging: a failed close degrades into
    a full-page refresh, and "the site put no close button" versus "it put one but the
    selector cannot recognise it" call for opposite handling, so without this list it is all
    guesswork — and loosening the click rules on a purchase dialog by guesswork is the last
    thing to do here. And this time, it was exactly that list (not any log message) that
    pointed to the real cause being a second dialog layer. The list is **captured every
    time**, but only printed "the first time this shape appears" (together with the
    layer-by-layer steps); for why and how it is captured, see `_report_dismiss_outcome`.

    **This function prints nothing itself**, it only accumulates facts; the narrative is
    produced solely by `_report_dismiss_outcome`. When adding things here, keep that division
    of labour — it is how the property "in steady state each block leaves only one line" is
    implemented.
    """
    escaped = False
    seen_before = None          # the dialog text still on screen after the previous round
    rounds: list[tuple[str, str]] = []   # (what this press was, the result after pressing)
    stop_reason = "rounds"      # the loop ran out without closing it → rounds used up
    for _round_no in range(1, max_rounds + 1):
        try:
            plan = port.execute_script(_DISMISS_DIALOG_JS)
        except port.TRANSPORT_ERRORS as error:
            _note_transport_error("dismiss dialog", error)
            return False
        if plan is None:
            # First round = there was no dialog to begin with (nothing to say, just return);
            # later = the previous round closed the last layer cleanly, and
            # `_BLOCKING_DIALOG_JS` and the picking `_DISMISS_DIALOG_JS` use different
            # criteria, so it can end up here.
            if not rounds:
                return True
            stop_reason = "closed"
            break
        # JS only **picks**; the pressing happens here. 'escape' (a string) = nothing picked.
        action, how = "escape", ""
        if isinstance(plan, dict):
            action = str(plan.get("action") or "clicked:?")
            how = _click_dismiss_target(port, plan.get("el"))
            # Which way it was pressed must go into the log: a real click succeeding vs
            # quietly falling back to a synthetic one is the only observation point for "does
            # this layer respond to synthetic events at all".
            action = f"{action} [{how or 'unclickable -> escape'}]"
        if not how:
            if escaped:
                stop_reason = "escape-spent"
                break           # Escape was already sent once; sending it again is only side effect
            escaped = True
            # Try the driver-level real key press first: a synthetic KeyboardEvent has
            # `isTrusted === false`, and some focus-trap libraries ignore it. The port not
            # having this method is not an error (the tests' fake ports and the slimmed-down
            # ports of other verification scripts may lack it); just skip it and carry on.
            real_escape = getattr(port, "press_escape", None)
            if callable(real_escape):
                try:
                    real_escape()
                except port.TRANSPORT_ERRORS as error:
                    _note_transport_error("dismiss dialog (real escape)", error)
            try:
                port.execute_script(_SEND_ESCAPE_JS)
            except port.TRANSPORT_ERRORS as error:
                _note_transport_error("dismiss dialog (escape)", error)
                return False
        human_pause(0.6, 1.2)
        # **Judge only after verifying, and write the log only after the whole loop ends.**
        # Each fixes one defect: the verdict used to announce "dismissed" before verifying
        # (the return value was always right, only the narrative was wrong — and people
        # reading the log judge by the narrative); the log used to print one line per round,
        # so every block produced `max_rounds` identical `could not dismiss … (layer N/4)`
        # lines. Now only facts are accumulated, and the narrative is left to
        # `_report_dismiss_outcome`.
        remaining = has_blocking_dialog(port)
        if remaining is None:
            rounds.append((action, "closed"))
            stop_reason = "closed"
            break
        if seen_before is None:
            # The first round has no baseline to compare against, so all it can say is "a
            # dialog is still blocking" — not "it changed".
            rounds.append((action, "still"))
        elif remaining == seen_before:
            # Something was pressed but the screen did not change a single character → this
            # button has no effect, and pressing again would be the same.
            rounds.append((action, "same"))
            stop_reason = "same"
            break
        else:
            rounds.append((action, "changed"))
        seen_before = remaining
    _report_dismiss_outcome(port, rounds, stop_reason, max_rounds)
    return stop_reason == "closed"


_DISMISS_VERDICT_TEXT = {
    "closed": "closed cleanly",
    "still": "a dialog is still blocking",
    "changed": "the dialog changed (there is another layer underneath)",
    "same": "the screen did not change a single character",
}
_DISMISS_STOP_TEXT = {
    "closed": "closed cleanly",
    "same": ("this press did not change the screen; not retrying (to avoid spinning through "
             "the whole set of rounds)"),
    "escape-spent": ("Escape was already sent once and sending it again is only side effect; "
                     "not closing further"),
    "rounds": "still blocked by a dialog after closing {max_rounds} layers in a row; not closing further",
}

# "I have printed this shape already". The key is a **digest**, not the raw text: the value
# is just a count, so the whole dict never grows to any meaningful size over the run time.
# **Per-process is deliberate** — reprinting once after a respawn is right: that is a new
# process, possibly new code, and possibly the first time after the site changed.
_DISMISS_LOG_SEEN: dict[str, int] = {}


def _report_dismiss_outcome(port, rounds: list[tuple[str, str]],
                            stop_reason: str, max_rounds: int) -> None:
    """The **only** log exit once the dismiss action has finished. The first time a given
    shape is met it prints everything; after that, one line.

    **Why the narrative is condensed.** After layered dismissing (rule 3) went live (from
    09-07 17:17), 25 blocked stretches were measured with **0 successful closes**, each one
    producing a fixed 2 lines of `could not dismiss … (layer N/4)` (50 lines in total) plus a
    whole control list — entirely predictable, zero information, and growing in proportion to
    `max_rounds`. This is exactly the opening shape of `discord_bot.log`'s 13,516/14,085 lines
    of `rpc apply -> ok` (96%), and `trim_log` keeps only the tail: **a log that pushes the
    useful messages out of the log file is worse than no log.**

    **What is condensed is the narrative, not the loop.** The loop is a generic fallback
    (`verify_quota_dialog.py` scenario 12 proves it really closes other dialog shapes), and
    not a single attempt was removed.

    Three properties, none of which can be dropped:

    1. **In steady state each block prints only one line**, and the line count does not grow
       with `max_rounds`.
    2. **"Closed it" and "could not close it" can still be told apart** — both paths carry
       the existing greppable strings `dismissed` / `could not dismiss` (historical
       statistics are counted by them).
    3. **The first time a shape is met there is still full information**: the press and the
       result of each layer, the reason for giving up, the control list — nothing missing.

    **The shape's definition includes the control list, and that is load-bearing.** The
    09-07 breakthrough of "it had actually closed the first layer, exposing a second" came
    not from any message, but from the **dialog size** in the list changing (856x917 →
    420x322). So the list is still captured every time (the failure path runs at most once
    per quota cycle, so its cost does not matter), it is just folded into the shape: the site
    changes → the shape changes → full logging resumes automatically.
    Only the success path does not capture it (the `happy path` should not pay for an extra
    JS round-trip).
    """
    dismissed = stop_reason == "closed"
    head = "dismissed" if dismissed else "could not dismiss"
    # On success the list is not captured (an extra JS round-trip is pointless); on failure
    # it is always captured, and what is captured counts towards the shape — see the
    # docstring.
    inventory = "" if dismissed else describe_dialog_controls(port)
    shape = "|".join(f"{a}>{v}" for a, v in rounds) + f"#{stop_reason}#{inventory}"
    key = hashlib.sha256(shape.encode("utf-8")).hexdigest()[:16]
    seen = _DISMISS_LOG_SEEN.get(key, 0) + 1
    _DISMISS_LOG_SEEN[key] = seen
    if seen > 1:
        print(f"  [quota] {head} blocking dialog ({len(rounds)} layers; occurrence {seen} of "
              f"the same shape {key} in this process, layer-by-layer steps identical to "
              f"occurrence 1, not reprinted)")
        return
    print(f"  [quota] {head} blocking dialog ({len(rounds)} layers, shape {key}): "
          + _DISMISS_STOP_TEXT[stop_reason].format(max_rounds=max_rounds))
    for i, (action, verdict) in enumerate(rounds, 1):
        print(f"  [quota]   layer {i} via {action!r} → "
              + _DISMISS_VERDICT_TEXT[verdict])
    if dismissed:
        return
    if inventory:
        print("  [quota] " + inventory)
    else:
        print("  [quota] the dialog control list could not be obtained — the dialog vanished "
              "at that instant, or JS could not get through.")


def wait_for_quota_recovery(port, batch_cfg: dict, *, label: str,
                            waited_sec: float = 0.0,
                            on_reload=None, on_idle=None) -> float:
    """When quota runs out: close the dialog → wait a while → return to the caller to retry
    the same image.

    Returns "the total seconds waited so far"; the caller must pass it back in, so that the
    `quota_wait_max_sec` cap accumulates across rounds. A caller that wants "how long the
    last round waited" just takes **the difference between the return value and the value
    passed in** (that is how `generate_loop` writes it into `quota_resumed.last_wait_sec`) —
    that is correct by definition, and the caller need not read the config again itself.

    **`quota_wait_poll_sec` is not a throughput knob.** See the measurements below; stop
    tuning it to go a bit faster.

    Design points (every one of them deliberate):
    - **Does not end the process**. The whole wait happens inside `generate_loop`, so the
      supervisor sees no rc, does not respawn, and does not rerun login + setup. The
      "automatically wait until quota recovers" users want only holds while the process is
      alive.
    - **Does not count towards `consecutive_fail_abort`**. Being blocked is not a failure, it
      is "not your turn yet".
    - **The poll interval is fixed (`quota_wait_poll_sec`), and must not become a backoff.**
      Measured 2026-09-07: the 214 `quota_blocked` in `events.ndjson` (08-24 → 09-07)
      happen to straddle two settings — about 14 minutes before 08-25 07:10, about 60 minutes
      after. Taking "between two adjacent `quota_blocked` of the same character" as the
      window and the `image_index` difference as output: the short-poll period (median window
      14.3 minutes, n=43) did **7.87 images/hour**, the long-poll period (median window 67.8
      minutes, n=151) **7.84 images/hour** — 194 windows, two weeks, a 0.4% difference.
      (Apply the same filter when recomputing: only windows ≤ 2 hours long. The long-poll
      period originally had 153, one of which was 36.5 hours long with only 6 images — the
      known 36-hour **downtime** gap, not a quota window; not filtering it out drags the
      sum-based figure to 6.50 and flips the conclusion entirely. The median is unaffected.)
      Across all 196 windows, the coefficient of variation of the **rate** is 0.131 while
      that of the **image count** is 0.427, and the correlation between window length and
      image count is r=0.118: what stays constant is the rate, what varies is the count —
      the shape of a "steady trickle", not a "batch released on the hour". In other words
      **the cap is on the account side, and the poll interval cannot affect it at all**.
      Shortening it only slices the same images into more, smaller bursts, and every wake-up
      pays for a `dismiss_blocking_dialog`, which was measured to fail 218/218 times and fall
      back to a full-page reload + an `on_reload` refill — exactly the trigger path of the
      most serious failure this project has had (fields partly not refilled, quietly burning
      10 images / two hours with the wrong prompt). Going from about 24 wake-ups a day to
      about 200 is **real downside, zero upside**.
    - **The pause marker is still honoured during the wait** (`wait_if_paused`), and `/stop`
      can still kill it.
    - The dialog is closed again every round: the site often pops it up again during the
      wait.
    - `quota_wait_max_sec` of 0 = no cap; when set positive and exceeded it raises
      `GenerationBlockedError`, falling back to the "stop cleanly, no respawn" path (that
      means this is not a quota that refills by itself, but a plan / account problem).
    - **`on_reload` is only called after a reload actually happened.** A reload knocks the
      page state back to the version the site persisted, and a prompt just filled in that
      the site has not saved yet vanishes entirely. The caller uses it to refill the fields;
      with no reload the state was not touched, and there is no need to pay that cost. See
      below for the measured basis.
    - **A liveness probe runs once per sleep slice** (`_probe_browser_alive`). A whole round
      issues no command at all to the driver for an hour by default, so if the browser dies
      during the wait, it is only found at the first command after the wait — up to an hour
      late, with the moment of death completely unknowable within that span. The probe pins
      it down to within 30 seconds. **It only records, and does not restart on the spot**;
      for why, see that function's docstring.
    - **`on_idle` runs once per sleep slice**, so interjected single-image requests are still
      served during the wait. Without it this whole stretch (an hour by default) is
      completely unresponsive to the user: the bot's `_SINGLE_IMAGE_PENDING_TTL_SEC` is only
      600 seconds, so after ten minutes it sweeps the request away as "the webrunner exited
      without serving it". And running out of quota is the norm on this account — measured,
      one cycle is about 68 minutes, 60 of them spent waiting — which means the user's
      instant image generation would almost always time out.
    """
    poll = float(batch_cfg.get("quota_wait_poll_sec", 3600.0))
    cap = float(batch_cfg.get("quota_wait_max_sec", 0.0))
    if cap > 0 and waited_sec >= cap:
        raise GenerationBlockedError(
            f"generation still blocked after waiting {waited_sec:.0f}s "
            f"(quota_wait_max_sec={cap:.0f}); this does not look like a quota "
            f"that refills — stopping instead of respawning.")
    dismissed = dismiss_blocking_dialog(port)
    if not dismissed:
        # Could not close it: refresh the page. A modal is page state, so a reload always
        # clears it; login uses a persisted cookie, so a reload does not log us out.
        print("  [quota] dialog would not close; reloading the page")
        try:
            port.refresh()
        except port.TRANSPORT_ERRORS as error:
            _note_transport_error("reload after blocked dialog", error)
        time.sleep(8.0)
        # After a reload the fields **must** be refilled. Measured (`WEBRunner.log`
        # 2026-08-24 to 08-27, 65 reloads): 61 happened after "this character had already
        # saved images", and all picked up smoothly; the only exception happened right after
        # the character's fields were filled and before a single image was produced, and every
        # Generate after that silently did nothing — no dialog, `get_main_image_src` returning
        # None for a full 180 seconds — burning 10 images and two hours in a row until
        # `consecutive_fail_abort` shut it down, with the abort message even wrongly blaming
        # "Chrome crashed". The difference was whether the site had managed to persist the
        # prompt just filled in.
        # This is not just about getting stuck: fields being **partly** knocked back (e.g.
        # only Character 2 lost) quietly produces a whole batch of images with the wrong
        # prompt, which is far worse than stopping.
        if on_reload is not None:
            on_reload()
    # Event throttling: the goal is "about one per hour", so a long wait does not flood the
    # channel. The round count must be **derived from `poll`**, not hard-coded — it used to
    # be hard-coded to every 6 rounds (exactly an hour with a 10-minute poll), and once the
    # default became 1 hour that turned into one report every six hours. **Round 0 is
    # skipped** — the caller already emitted `quota_blocked` at the moment of being blocked,
    # and another one at the same moment would be posting the same thing twice.
    #
    # This relies on **every round being the same length**, and that premise holds:
    # `batch_cfg` is re-read by `run_batch` once per character and passed whole into
    # `generate_loop`, and the total of one quota wait falls entirely within one character,
    # so `poll` cannot change value partway through the total.
    every = max(1, round(3600.0 / poll)) if poll > 0 else 1
    cycle = int(waited_sec // poll) if poll > 0 else 0
    if cycle > 0 and cycle % every == 0:
        emit_event("quota_wait", label=label, waited_sec=round(waited_sec, 1),
                   next_retry_sec=round(poll, 1))
    print(f"  [quota] generation blocked during {label}; waited "
          f"{waited_sec / 60:.0f} min so far, retrying in {poll / 60:.0f} min")
    # Sleep in slices: gives the pause marker and `/stop` a chance to take effect during the
    # wait, instead of blocking for a whole round.
    remaining = poll
    browser_alive = True
    while remaining > 0:
        wait_if_paused(f"{label} (quota wait)")
        # Liveness probe. It goes at the very start of the slice: it is the cheapest
        # round-trip here, and asking it first pins the moment of death to within 30 seconds,
        # instead of waiting for `on_idle` to spit out a pile of knock-on failure messages
        # first. **Only the "alive → dead" transition is logged**: this loop runs 120 turns
        # by default, and a line per turn is no signal at all (this project has recorded the
        # same principle twice). Likewise no event is emitted — that would flood the chat
        # platform, and a new event type needs all three sides pulled together; the value
        # here is diagnosability, and the log is enough.
        gone = _probe_browser_alive(port)
        if gone is None:
            browser_alive = True
        elif browser_alive:
            browser_alive = False
            elapsed = poll - remaining
            print(f"  [quota] {time.strftime('%Y-%m-%d %H:%M:%S')} the browser died while "
                  f"waiting for quota (waited {elapsed:.0f}s this round, "
                  f"{waited_sec + elapsed:.0f}s in total): {gone}. Only recording it, not "
                  f"restarting on the spot — a restart means logging in again and refilling "
                  f"the fields, so it is left to the existing recovery flow at its usual time.",
                  file=sys.stderr)
        # Stay responsive to the user during this hour of waiting for quota; see `on_idle`
        # in the docstring.
        if on_idle is not None:
            on_idle()
        slice_sec = min(30.0, remaining)
        time.sleep(slice_sec)
        remaining -= slice_sec
    return waited_sec + poll


def _abort_if_generation_blocked(port, where: str) -> None:
    """Raise `GenerationBlockedError` on a Tier 1 hit (→ stop cleanly, no respawn).

    The dialog's full text is written only to the log (stderr) — it is the site's raw
    string, outside our control, and under CLAUDE.md's secrecy rules must not be sent to the
    chat platform. Incidentally, that log line is the only basis for coming back to tighten
    the `_GENERATION_BLOCK_JS` patterns.

    **The line after the text prints the full length and the margin to `_TIER1_TEXT_CAP`.**
    Not one more character of content goes into the log (it is still `slice(0, 400)`); the
    only addition is an integer. The question it answers is "how much margin does this cap
    have left": the site's paywall is a whole pricing table, one more plan row could cross
    1200, and **crossing it is silent** — Tier 1 simply treats it as no dialog. With the
    margin as a measured number, there is no need to guess next time.
    """
    hit = get_generation_block(port)
    if not hit:
        return
    text = str(hit.get("text", ""))[:400]
    print(f"  [blocked] generation is blocked by a purchase/account dialog "
          f"during {where}; matched {hit.get('pattern')!r}", file=sys.stderr)
    print(f"  [blocked] dialog text: {text!r}", file=sys.stderr)
    full_len = hit.get("length")
    if isinstance(full_len, int):
        print(f"  [blocked] dialog innerText full length {full_len} chars "
              f"(Tier 1 skips anything over {_TIER1_TEXT_CAP}; margin "
              f"{_TIER1_TEXT_CAP - full_len})", file=sys.stderr)
    raise GenerationBlockedError(
        f"generation blocked during {where} (matched {hit.get('pattern')!r}); "
        f"stopping instead of respawning — needs a human.")


def _is_chrome_crash_page(port) -> bool:
    """Detect the Chrome renderer crash interstitial (the "Aw, Snap!"
    page).

    When Chrome's tab renderer crashes the chromedriver session itself
    stays alive — `execute_script` keeps responding — but the page DOM
    is gone, replaced with the crash page. Hot-path readers
    (`get_main_image_src`, `find_generate_button` …) all return None,
    `generate_one_image` chews through its 4× retries, and the loop's
    `consecutive_fails` counter ticks. Without this detector, the run
    waits the full `consecutive_fail_abort` (~5 min default) before
    `generate_loop` raises and the supervisor can respawn Chrome.

    Detecting the interstitial directly lets us short-circuit to a
    supervisor respawn on the very first failed image, saving the
    intervening 4-5 minutes of empty retries.

    Wrapped in `port.TRANSPORT_ERRORS` because if chromedriver itself
    is also dead (not just the renderer), reading `current_url` /
    `title` would re-trigger the same transport stall we already
    mitigate elsewhere — treat the failure as "not crashed" so the
    caller falls back to its existing fail-counter path."""
    try:
        url = port.current_url() or ""
        title = (port.get_title() or "").lower()
    except port.TRANSPORT_ERRORS as error:
        # Just a hiccup → treat it as "not crashed" and fall back to the caller's failure
        # counter as before; the window / session really gone → `_note_transport_error`
        # escalates it to BrowserGoneError.
        _note_transport_error("chrome crash probe", error)
        return False
    if "chrome-error://" in url:
        return True
    return any(marker in title for marker in _CHROME_CRASH_TITLE_MARKERS)


def _try_chrome_refresh(port) -> bool:
    """Best-effort `port.refresh()` after a detected crash interstitial.

    The supervisor is going to kill-and-respawn Chrome anyway (caller
    raises a `RuntimeError` right after this), so the refresh is a
    courtesy — gives Chrome a chance to clear the crashed renderer's
    state so the next user gets a cleaner profile. Failure here is
    fine; we still raise."""
    try:
        port.refresh()
        time.sleep(5.0)  # let the page settle before any post-snap
        return True
    except port.TRANSPORT_ERRORS as error:
        # This deliberately does **not** escalate to BrowserGoneError: the caller is about to
        # raise anyway, and the refresh is only clean-up. Squashed into one line so
        # chromedriver's Stacktrace does not flood the log.
        print(f"  [warn] refresh after chrome crash failed: "
              f"{_short_error(error)}")
        return False


def _abort_if_chrome_crashed(port, where: str) -> None:
    """Raise `RuntimeError` (→ supervisor respawn) if the page is currently
    the Chrome crash interstitial.

    `generate_loop` already short-circuits on the crash page inside its
    per-image fail branch, but a renderer crash during the SETUP / fill
    phase has no such guard: every `with_retry` step just burns its 3
    attempts on the dead DOM, `fill_char1` failures `continue` to the next
    pair, and the run can grind through every pair saving 0 images and then
    exit rc=0 — the supervisor sees a "clean finish" and never respawns.
    Calling this at the setup boundary and on fill failures turns that
    silent dead-end into an abort the supervisor can recover from.

    Checks TWO deaths, in order: the session being gone entirely (window
    closed / target destroyed — `_abort_if_browser_gone`), then the crash
    interstitial (session alive, DOM replaced). The order matters: on a gone
    session the crash-page probe can only report "couldn't read, assume no
    crash", so it would never fire."""
    _abort_if_browser_gone(port, where)
    if _is_chrome_crash_page(port):
        snap(port, f"chrome_crash_{where}")
        _try_chrome_refresh(port)
        raise RuntimeError(
            f"Chrome renderer crashed (interstitial detected) during "
            f"{where}; aborting so supervisor respawns Chrome."
        )


def click_via_js(port, element) -> None:
    port.execute_script("arguments[0].click();", element)


# The literal candidates for the "reject" button on the consent banner, matched exactly, in
# order.
#
# Measured on the site's home page on 2026-08-30 with a brand-new temporary profile: the banner
# has only two buttons, labelled `Accept All` and `Reject Non-Essential` — **the site has no
# `Reject All` at all**. The original primary rule hard-coded a match on `Reject All`, so it
# never hit even once; the banner was actually being closed by the loose "contains Reject"
# fallback below. Yet another case of "the fallback is taken every time = the primary path is
# dead" — the difference this time being that the fallback really works, so everything looks
# perfectly normal.
#
# Why fix it anyway: once the fallback is tightened (e.g. someone switches it to an exact
# match to avoid misclicks), consent handling fails silently, and the symptom is random click
# failures caused by the banner covering the page, far away from the cause.
COOKIE_REJECT_LABELS = ("Reject Non-Essential", "Reject All")

_COOKIE_CONSENT_JS = r"""
const labels = arguments[0];
const btns = Array.from(document.querySelectorAll('button'));
const labelOf = b => (b.innerText || '').replace(/\s+/g, ' ').trim();
// 1) Known labels, exact match.
for (const want of labels) {
  for (const b of btns) {
    if (b.offsetParent !== null && labelOf(b) === want) {
      b.click();
      return {clicked: want, present: true, ready: true};
    }
  }
}
// 2) Fallback: any button containing `Reject`. It still catches the case where the site
// changes the label.
for (const b of btns) {
  const t = labelOf(b);
  if (b.offsetParent !== null && /reject/i.test(t)) {
    b.click();
    return {clicked: t, present: true, ready: true};
  }
}
// Nothing clicked. Report "is there a consent prompt on this page at all", so the caller
// can leave early instead of waiting idly until the timeout. Only **action-type** labels
// count, and the `^` anchor is essential: the site's footer permanently carries a
// `Manage Cookie Preferences` button, and without the anchor it always matches, the
// early-exit condition never holds, and this optimisation silently stops working
// (measured 2026-08-30: it is still there after the banner is closed).
const ACTION = /^(reject|accept|allow|agree|decline|deny)\b/i;
let present = false;
for (const b of btns) {
  if (b.offsetParent !== null && ACTION.test(labelOf(b))) { present = true; break; }
}
return {clicked: null, present: present,
        ready: document.readyState === 'complete'};
"""


def reject_cookies(port, timeout: float = 15.0, settle: float = 2.0) -> bool:
    """Close the cookie consent banner. When there is no banner, return **as quickly as
    possible** instead of waiting idly until the timeout.

    The production profile is persistent and consent was saved long ago, so "no banner" is the
    norm — measured, all 16/16 runs printed `cookie banner not found`, each burning the full 5
    seconds (login page) and 15 seconds (generation page), so every setup wasted 20 seconds.

    The early-exit condition is deliberately conservative: it requires
    `document.readyState === 'complete'`, and no action-type consent button seen for `settle`
    consecutive seconds. When a banner is present but not yet clickable (present=True) the
    condition does not hold, and it waits out the full `timeout` as before.
    """
    # Both measure "how much time has passed" → always use the **monotonic** clock.
    # `time.time()` on this machine is `GetSystemTimePreciseAsFileTime()` with
    # `adjustable=True`; NTP step corrections / changing the clock by hand / restoring a VM
    # snapshot all make it jump forwards or backwards (**changing time zone and daylight
    # saving time do not** — it returns UTC epoch seconds); measuring an interval with it, a
    # jump back of an hour means this loop spins for an extra hour, and a jump forward
    # declares "definitely no banner" before settle has elapsed.
    end = time.monotonic() + timeout
    quiet_since: float | None = None
    while time.monotonic() < end:
        state = port.execute_script(_COOKIE_CONSENT_JS,
                                    list(COOKIE_REJECT_LABELS)) or {}
        clicked = state.get("clicked")
        if clicked:
            human_pause(0.4, 0.8)
            print(f"cookie consent -> {clicked!r}")
            return True
        if state.get("ready") and not state.get("present"):
            if quiet_since is None:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= settle:
                # Deliberately worded differently from the timeout message below: one is
                # "definitely none", the other "still unsure at the very end", and whoever
                # reads the log must be able to tell which.
                print("no cookie consent prompt on this page; skipping")
                return False
        else:
            quiet_since = None
        time.sleep(0.3)
    print("cookie banner not found; skipping")
    return False


def _find_model_trigger(port):
    return port.execute_script(
        """
        const inp = document.querySelector("input[aria-label='Select the Model']");
        if (inp) {
          let el = inp;
          for (let i = 0; i < 6 && el; i++) {
            const cs = window.getComputedStyle(el);
            if (cs.cursor === 'pointer' || el.onclick || el.getAttribute('role') === 'button') {
              return el;
            }
            el = el.parentElement;
          }
          return inp.parentElement;
        }
        const candidates = [];
        for (const el of document.querySelectorAll('div, button, span')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          if (t.startsWith('NAI Diffusion')) candidates.push([el, t.length]);
        }
        candidates.sort((a, b) => a[1] - b[1]);
        if (!candidates.length) return null;
        let el2 = candidates[0][0];
        for (let i = 0; i < 5 && el2; i++) {
          const cs = window.getComputedStyle(el2);
          if (cs.cursor === 'pointer' || el2.onclick || el2.getAttribute('role') === 'button') {
            return el2;
          }
          el2 = el2.parentElement;
        }
        return candidates[0][0];
        """
    )


def _dump_model_options(port) -> None:
    """Print the model options currently visible in the dropdown into the log (stderr / the
    log file, never leaking to the chat platform).

    When the site rewrites the option labels, this log line is the only evidence that
    directly shows "what `model_candidates` should be changed to" — without it all that is
    left is a screenshot to compare by hand. Purely diagnostic, never raises."""
    try:
        names = port.execute_script(
            """
            const out = [];
            for (const el of document.querySelectorAll('[role="option"], li, [role="listitem"], div')) {
              if (el.offsetParent === null) continue;
              const firstLine = (el.innerText || '').split('\\n')[0].trim();
              if (!firstLine || firstLine.length > 60) continue;
              if (!/Diffusion|NAI|Anime|Furry/i.test(firstLine)) continue;
              out.push(firstLine);
            }
            return Array.from(new Set(out)).slice(0, 30);
            """
        )
    except Exception as error:  # pylint: disable=broad-except
        print(f"  [warn] could not dump model options: {type(error).__name__}")
        return
    print(f"  visible model options: {names}")


def select_model(port, target, timeout: float = 12.0) -> bool:
    """Select `target` in the model dropdown; returns True on success.

    `target` can be a single string or a **sequence of candidate labels** — the site
    occasionally rewrites the option text (this round it changed from `V4.5 Full` to
    `V5 Full`); they are tried in order and it stops at the first hit, so callers can pass
    old and new labels together without touching the logic here.

    Matching stays "the option's first line is **exactly equal**", deliberately not a prefix
    match: a prefix like `NAI Diffusion V5` would hit both Full and Curated, and selecting the
    wrong model is harder to notice than selecting none (images still come out, just in the
    wrong style).

    When every candidate misses, besides `snap()` as before, `_dump_model_options()` also
    writes the options visible at that moment into the log.
    """
    candidates = [target] if isinstance(target, str) else [t for t in target if t]
    # The timeout is an **interval** → monotonic clock (see `reject_cookies` for details). A
    # wall clock jumping back turns a DOM timeout of a few seconds into a stall of hours —
    # **getting stuck is harder to diagnose than failing**, since the supervisor sees a
    # process that is alive yet doing nothing; jumping forward degrades it into a single try.
    end = time.monotonic() + timeout
    trigger = None
    while time.monotonic() < end:
        trigger = _find_model_trigger(port)
        if trigger:
            break
        time.sleep(0.3)
    if not trigger:
        snap(port, "no_model_selector")
        return False
    port.execute_script("arguments[0].scrollIntoView({block:'center'});", trigger)
    print(f"model trigger tag={trigger.tag_name} text={trigger.text[:60]!r}")
    port.click(trigger)
    human_pause(0.6, 1.1)
    # Same as above: waiting for the options to appear is also an interval → monotonic clock.
    option_end = time.monotonic() + timeout
    while time.monotonic() < option_end:
        clicked = port.execute_script(
            """
            const targets = arguments[0];
            const lists = Array.from(document.querySelectorAll('[role="option"], li, [role="listitem"], div'));
            for (const want of targets) {
              for (const el of lists) {
                if (el.offsetParent === null) continue;
                const firstLine = (el.innerText || '').split('\\n')[0].trim();
                if (firstLine === want) {
                  el.scrollIntoView({block:'center'});
                  el.click();
                  return want;
                }
              }
            }
            return null;
            """,
            candidates,
        )
        if clicked:
            human_pause(0.4, 0.8)
            print(f"  model -> {clicked!r}")
            return True
        time.sleep(0.3)
    print(f"  no model option matched any candidate: {candidates}")
    _dump_model_options(port)
    snap(port, "model_option_not_found")
    return False


def fill_textarea_like(port, element, text: str) -> bool:
    """Fill textarea / input / contenteditable with `text`, atomic replace.
    Does not use `.send_keys(text)`, nor the two-step "clear, then write" (that lets React
    commit an empty string in between and then re-render over the value we wrote — the main
    prompt being cleared again right after it was filled is exactly this race).

    Mechanism:
    - textarea / input: write the value in one go with
      `Object.getOwnPropertyDescriptor(...).value.set` (bypassing React's value tracking),
      `dispatchEvent('input')` + `'change'` so React sees a user-input event and syncs the
      component state, and finally `blur()` to force a commit.
    - contenteditable: select the existing content → `document.execCommand('insertText', …)`.
      This path is the approach recommended by frameworks such as
      @testing-library/user-event; it runs the full `beforeinput` / `input` event sequence,
      and React's onChange receives it normally. execCommand is deprecated, but Chrome still
      supports it, and it is currently the most reliable method. Only as a second-best does
      it fall back to `innerText = val` + dispatchEvent.

    Anti-bot imitation is handled by the main loop's `human_pause(0.8, 1.5)` "between
    different fills"; a single fill does not need to pretend to be a human "character by
    character".

    Flow: (1) scroll + click to get focus (2) atomic replace in one write (3) blur to force a
    React commit (4) verify (5) if it does not match, re-click + write once more (6) if it
    still does not match, only print a WARN; the caller's `with_retry` gives it another
    chance.
    """
    try:
        port.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", element,
        )
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    try:
        element.click()
    except Exception:  # pylint: disable=broad-except
        try:
            port.execute_script("arguments[0].focus();", element)
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
    time.sleep(random.uniform(0.15, 0.35))
    # Atomic replace (handles both the textarea and contenteditable paths internally).
    _fill_via_native_setter(port, element, text)
    # After writing, close the autocomplete dropdown before blurring, so a leftover dropdown
    # is not hit by a later click.
    _dismiss_autocomplete(port, element)
    # Force a React commit: blur triggers the onBlur handler / component lifecycle.
    try:
        port.execute_script("arguments[0].blur();", element)
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    time.sleep(random.uniform(0.3, 0.6))
    actual = _read_textarea_value(port, element).strip()
    expected = text.strip()
    if actual == expected:
        return True
    print(
        f"  fill verify mismatch: got {len(actual)}/{len(expected)} chars; "
        f"retrying once"
    )
    try:
        element.click()
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    time.sleep(random.uniform(0.2, 0.4))
    _fill_via_native_setter(port, element, text)
    # The retry path reopens the dropdown too, so likewise Escape first, then blur.
    _dismiss_autocomplete(port, element)
    try:
        port.execute_script("arguments[0].blur();", element)
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    time.sleep(random.uniform(0.3, 0.6))
    actual2 = _read_textarea_value(port, element).strip()
    if actual2 != expected:
        print(
            f"  WARN: fill still mismatched after retry "
            f"({len(actual2)} vs {len(expected)} chars)"
        )
        # Automatic diagnosis: dump the DOM textarea attributes into the log, so the next
        # debugging session need not wait for the user to take a screenshot or report back.
        for line in format_textareas_diag(dump_textareas_diag(port)).split("\n"):
            print(f"    {line}")
        return False
    return True


def _fill_via_native_setter(port, element, text: str) -> None:
    """Atomic replace via React-friendly mechanisms. One write — no separate "clear" and
    "write" steps, so React cannot re-render and commit an empty string in between.

    - textarea / input: prototype value setter + input/change events
    - contenteditable: selectNodeContents + execCommand('insertText'); only if execCommand
      fails does it fall back to setting innerText + an InputEvent

    Any JS exception is swallowed + one line printed; the caller verifies and retries.
    """
    try:
        port.execute_script(
            """
            const e = arguments[0], val = arguments[1];
            try { e.focus(); } catch (_) {}
            if (e.tagName === 'TEXTAREA' || e.tagName === 'INPUT') {
                const proto = e.tagName === 'TEXTAREA'
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(e, val);
                e.dispatchEvent(new Event('input', {bubbles: true}));
                e.dispatchEvent(new Event('change', {bubbles: true}));
            } else {
                // Contenteditable: select all → execCommand('insertText') atomic
                // replace. execCommand fires the full beforeinput / input event
                // chain, which is what makes React's onChange commit.
                try {
                    const range = document.createRange();
                    range.selectNodeContents(e);
                    const sel = window.getSelection();
                    sel.removeAllRanges();
                    sel.addRange(range);
                } catch (_) {}
                let ok = false;
                try { ok = document.execCommand('insertText', false, val); }
                catch (_) { ok = false; }
                if (!ok) {
                    // Fallback: where execCommand is unavailable, write innerText in one go.
                    e.innerText = val;
                    e.dispatchEvent(new InputEvent('input', {
                        bubbles: true, data: val, inputType: 'insertText'
                    }));
                }
            }
            """,
            element, text,
        )
    except Exception as error:  # pylint: disable=broad-except
        # `_short_error`: `fill_textarea_like` calls this at most twice per fill (the first
        # write + the rewrite after a mismatch), and the fill itself is wrapped in
        # `with_retry` retries.
        print(f"  _fill_via_native_setter exception: {_short_error(error)}",
              file=sys.stderr)


def driver_version_line(capabilities) -> str:
    """Put the browser and driver versions "this session really used" into one line. Never
    raises.

    Versions always come from the capabilities the driver reports itself, not from the
    registry or the executables — what matters is **the two things this spawn actually
    connected to**, not "what is installed on the machine". Anything unavailable is `?`.

    When the major versions differ, a warning is appended. That path is normally unreachable
    (chromedriver refuses to drive a Chrome with a different major version, so the spawn fails
    first); it stays because it is cheap, and should that check ever be relaxed, it is exactly
    the one thing one would want to see at that moment.

    **Where the key names come from, spelled out so they are not taken as verified**:
    `browserVersion` is the W3C standard capability name, findable in the installed selenium
    source (`options.py`'s `_BaseOptionsDescriptor`); `chromedriverVersion` is put by
    chromedriver itself into the `chrome` sub-dict it returns, and **cannot be measured
    without opening a browser** — and on this machine a production batch is almost always
    running, while opening a second Chrome is exactly why `_chrome_slot` exists. So the
    design here is "a wrong guess breaks nothing": anything unavailable prints `?`, that line
    itself tells you the key name needs fixing, and it never affects a spawn that already
    succeeded.
    """
    caps = capabilities if isinstance(capabilities, dict) else {}
    browser = str(caps.get("browserVersion") or "?").strip() or "?"
    driver = "?"
    chrome = caps.get("chrome")
    if isinstance(chrome, dict):
        raw = str(chrome.get("chromedriverVersion") or "").strip()
        driver = raw.split(" ", 1)[0] or "?"
    line = f"chrome {browser} / chromedriver {driver}"
    b_major, _, _ = browser.partition(".")
    d_major, _, _ = driver.partition(".")
    if b_major.isdigit() and d_major.isdigit() and b_major != d_major:
        line += "  ** major version mismatch **"
    return line


def log_driver_versions(capabilities) -> str:
    """Print `driver_version_line(...)` and return it. Never raises — this is a one-line
    record after a successful spawn, and must not have any chance of turning a successful
    spawn into a failure.

    **Why it is worth a line**: from September 2026 Chrome moved to **a major version every
    two weeks**, and chromedriver's major version must match exactly, otherwise
    `webdriver.Chrome(...)` raises `SessionNotCreatedException`. This project really hit that
    on 2026-08-25, and the log held only
    `Chrome spawn attempt 1/3 failed: SessionNotCreatedException()`, nothing else.
    ⚠️ **This function's original rationale said "that exception's message is empty" — it was
    overturned by measurement on 2026-09-12 and has been withdrawn.** What is empty is
    `args`, not the message: selenium puts the message in `self.msg`, so `repr(e)` returns
    `SessionNotCreatedException()` while `str(e)` has 351 characters, including both version
    numbers. That log line was thrown away by **our own `{err!r}` format**, and since
    2026-09-12 it is handled by `full_error_detail`. **This is a lesson in itself**: before
    adding new diagnostics for a conclusion of "the diagnostic data does not exist", first
    confirm that your own formatting did not eat it — otherwise the new mechanism masks the
    original defect, while the original defect lives on in other paths (measured at the
    time: 7 more sites of the same shape).
    This line is still worth keeping, but for a different reason: it records the versions as
    soon as a spawn **succeeds**, so the moment of failure need not depend on any exception
    formatting, and `restart_chrome_every_n_characters` defaults to 1 = recorded afresh for
    every character. This machine runs unattended for days, and the log is the only forensic
    record.
    """
    line = driver_version_line(capabilities)
    print(f"  [driver] {line}")
    return line


def _dismiss_autocomplete(port, element) -> None:
    """Close NovelAI's tag autocomplete dropdown.

    After typing, the caret sits at the end of the text and NovelAI pops up a suggestion
    dropdown for the last token; if it is not closed, later coordinate clicks (expanding the
    Character section, pressing Generate…) may hit the dropdown and insert a suggested tag at
    the end of the prompt. The synthetic Escape carries keyCode/which=27 for compatibility
    with React's legacy event handling. Failures are swallowed — a dropdown left open must not
    blow up the run either; verify_character_prompt before generation is the second line of
    defence."""
    try:
        port.execute_script(
            """
            const e = arguments[0];
            const ev = t => new KeyboardEvent(t, {
                key: 'Escape', code: 'Escape', keyCode: 27, which: 27,
                bubbles: true, cancelable: true});
            e.dispatchEvent(ev('keydown'));
            e.dispatchEvent(ev('keyup'));
            """,
            element,
        )
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass


def find_prompt_areas(port):
    xpath = (
        "//*[(self::textarea or @contenteditable='true')"
        " and not(@aria-label='Select a language')]"
    )
    return [el for el in port.find_elements_xpath(xpath) if el.is_displayed()]


def fill_main_prompt(port, text: str) -> bool:
    areas = find_prompt_areas(port)
    if not areas:
        snap(port, "no_prompt_area")
        return False
    main_area = areas[0]
    try:
        main_area.click()
    except Exception:  # pylint: disable=broad-except
        pass
    human_pause(0.2, 0.5)
    return fill_textarea_like(port, main_area, text)


def find_undesired_textarea(port):
    """Locate the main "Undesired Content" textarea on NovelAI.

    NovelAI's React DOM obfuscates the styled-component class names, and they change with
    every deploy, so this looks for it with three layers of fallback:
      1. textarea / contenteditable whose `aria-label` contains "undesired"
      2. the same elements, but matched by `placeholder`
      3. find the label with the text "Undesired Content", then the next textarea after it

    **The main prompt element is excluded** (the first visible item of `find_prompt_areas()`)
    — it has been observed that on some deploys the main prompt's placeholder contains
    "undesired" (showing a hint along the lines of "Add a prompt or click to choose Undesired
    Content..."), which would be a false hit. So any step that matches the main prompt's
    element rejects it outright and keeps looking for the next one.

    If everything fails it `snap()`s a debug image and returns None; the caller should treat
    that as "skip the fill" rather than abort the whole run (NovelAI keeps the undesired
    value left over from the previous round).
    """
    main_areas = find_prompt_areas(port)
    main_area = main_areas[0] if main_areas else None

    def _acceptable(el) -> bool:
        if not el.is_displayed():
            return False
        if main_area is not None and el == main_area:
            return False
        return True

    # 1. aria-label (the most reliable; NovelAI generally sets it)
    candidates = port.find_elements_xpath(
        "//*[(self::textarea or @contenteditable='true')"
        " and contains("
        "translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
        "'undesired')]"
    )
    for el in candidates:
        if _acceptable(el):
            return el
    # 2. placeholder
    candidates = port.find_elements_xpath(
        "//*[(self::textarea or @contenteditable='true')"
        " and contains("
        "translate(@placeholder,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
        "'undesired')]"
    )
    for el in candidates:
        if _acceptable(el):
            return el
    # 3. The first textarea / contenteditable after the 'Undesired Content' label, grabbed
    # directly with the XPath `following::` axis, with no need to climb the DOM tree.
    candidates = port.find_elements_xpath(
        "//*[normalize-space(text())='Undesired Content']"
        "/following::*[self::textarea or @contenteditable='true'][1]"
    )
    for el in candidates:
        if _acceptable(el):
            return el
    snap(port, "no_undesired_textarea")
    # Automatic diagnosis: dump straight away when nothing is found, so selector adjustments
    # have something to go on.
    print("find_undesired_textarea: no acceptable element after 3 fallbacks")
    for line in format_textareas_diag(dump_textareas_diag(port)).split("\n"):
        print(f"  {line}")
    return None


def fill_main_undesired(port, text: str) -> bool:
    """Fill NovelAI's main undesired content (negative prompt) textarea.
    Returns False when it cannot be found — no exception, no aborting the run; the caller
    should keep generating, and NovelAI uses the value left over from the previous round. An
    empty `text` still clears that textarea."""
    el = find_undesired_textarea(port)
    if el is None:
        return False
    try:
        port.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    except Exception:  # pylint: disable=broad-except
        pass
    try:
        el.click()
    except Exception:  # pylint: disable=broad-except
        pass
    human_pause(0.2, 0.4)
    return fill_textarea_like(port, el, text)


def _click_gender(port, gender: str, timeout: float = 4.0) -> bool:
    """Click the gender option that pops up after adding a character (V4.5: Female / Male /
    …; V5: Female / Male / Other).

    Does not use JS's `el.click()`: like the sampler dropdown, that menu may listen only to
    mousedown, so a JS click is **silently ineffective** (see `robust_click`). Instead it
    gets the element first and then goes through a real mouse click. It takes the one with
    the shortest `outerHTML`, to avoid clicking an outer wrapper that merely contains the
    option.
    """
    # The timeout is an **interval** → monotonic clock (see `reject_cookies` for details). A
    # wall clock jumping back turns a DOM timeout of a few seconds into a stall of hours —
    # **getting stuck is harder to diagnose than failing**, since the supervisor sees a
    # process that is alive yet doing nothing; jumping forward degrades it into a single try.
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        el = port.execute_script(
            """
            const want = arguments[0];
            let best = null, bestLen = Infinity;
            for (const el of document.querySelectorAll(
                   'div, li, button, span, [role="option"]')) {
              if (el.offsetParent === null) continue;
              if ((el.innerText || '').trim() !== want) continue;
              const len = el.outerHTML.length;
              if (len < bestLen) { best = el; bestLen = len; }
            }
            return best;
            """,
            gender,
        )
        if el is not None:
            robust_click(port, el)
            human_pause(0.3, 0.6)
            return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# Locating character cards (the core of the V4.5 → V5 migration)
#
# V4.5: the character name is a plain text node (`innerText === 'Character N'`), and **only
#       the currently focused card** shows its prompt field, so the old code could simply
#       use `find_prompt_areas()[1]` as "the field of the current character".
# V5  : the character name became a **renameable `<input>`**, with the name in
#       `placeholder` and `innerText` an empty string — every detection relying on
#       innerText **silently returns 0 / not found** (no error); and once expanded, **every
#       card's prompt field is visible at the same time**, so `find_prompt_areas()` returns
#       `[main prompt, char1, char2, …]`.
#
# So everything here is changed to "locate the character card first, then take that card's
# own prompt field". Index arithmetic (`areas[1]`) under V5 writes into **the wrong card**,
# and being wrong raises no error, it just produces images with the characters mixed up —
# the kind of failure hardest to notice from the results.
#
# Also: V5's DOM is **double-rendered** for desktop / mobile, so the same character input
# appears twice, one copy with `offsetParent === null`. Every scan must filter out the
# invisible copy, or the character count doubles.
# ---------------------------------------------------------------------------

# Fills in the complete pointer/mouse event sequence. A fallback for components that "only
# listen to mousedown / pointerdown" — a plain `el.click()` is **silently** ineffective on
# them.
_MOUSE_SEQUENCE_JS = """
const el = arguments[0];
const opts = {bubbles: true, cancelable: true, view: window};
for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
  const Ctor = (type.indexOf('pointer') === 0 && window.PointerEvent)
             ? window.PointerEvent : window.MouseEvent;
  try { el.dispatchEvent(new Ctor(type, opts)); } catch (e) {}
}
return true;
"""

# Shared JS for character cards. Every script that needs it prepends this block
# (`execute_script` gets a brand-new scope every time, with no cross-call global to put it in).
_JS_CHAR_HELPERS = r"""
function __charNameEls() {
  // V5 first: the renameable input, with the name in placeholder.
  const out = [];
  for (const el of document.querySelectorAll('input')) {
    if (el.offsetParent === null) continue;
    const ph = (el.getAttribute('placeholder') || '').trim();
    if (/^Character \d+$/.test(ph)) out.push([el, ph]);
  }
  if (out.length) return out;
  // V4.5 fallback: a plain-text heading.
  for (const el of document.querySelectorAll('div, span, button, label')) {
    if (el.offsetParent === null) continue;
    const t = (el.innerText || '').trim();
    if (/^Character \d+$/.test(t)) out.push([el, t]);
  }
  return out;
}
function __findCharName(want) {
  const all = __charNameEls();
  for (let i = 0; i < all.length; i++) {
    if (all[i][1] === want) return all[i][0];
  }
  return null;
}
function __charHeaderRow(nameEl) {
  // Header row = the first ancestor up the tree with >=3 buttons (up / down / check / trash /
  // unfold).
  let el = nameEl;
  for (let i = 0; i < 10 && el; i++) {
    el = el.parentElement;
    if (!el) break;
    if (el.querySelectorAll('button').length >= 3) return el;
  }
  return null;
}
function __charCard(nameEl) {
  // Card = the first ancestor up the tree that "contains at least one visible prompt field";
  // but if that ancestor also wraps the name field of more than one card, we went too far
  // (that is the whole Character Prompts section), so return null to let the caller know
  // this card is currently collapsed.
  let el = nameEl;
  for (let i = 0; i < 12 && el; i++) {
    el = el.parentElement;
    if (!el) break;
    let areas = 0;
    for (const a of el.querySelectorAll("textarea, [contenteditable='true']")) {
      if (a.offsetParent !== null) areas++;
    }
    if (!areas) continue;
    let names = 0;
    for (const n of el.querySelectorAll('input')) {
      if (n.offsetParent === null) continue;
      const ph = (n.getAttribute('placeholder') || '').trim();
      if (/^Character \d+$/.test(ph)) names++;
    }
    if (names > 1) return null;
    return el;
  }
  return null;
}
function __cardIcon(nameEl, iconName) {
  // V5's card buttons have no aria-label / title, but the file names of the CSS masks are
  // meaningful: directional_arrow_up / directional_arrow_down / check / trash / unfold.
  // The file names carry a content hash (trash.72ef2ba9.svg), so only the **base name** is
  // compared.
  const row = __charHeaderRow(nameEl);
  if (!row) return null;
  for (const b of row.querySelectorAll('button')) {
    if (b.disabled || b.offsetParent === null) continue;
    const nodes = [b].concat(Array.from(b.querySelectorAll('*')));
    for (const node of nodes) {
      const st = window.getComputedStyle(node);
      const mi = ((st.maskImage || '') + ' ' + (st.webkitMaskImage || '')
                  + ' ' + (st.backgroundImage || '')).toLowerCase();
      if (mi.indexOf(iconName) !== -1) return b;
    }
  }
  return null;
}
"""


def robust_click(port, element) -> bool:
    """A real mouse click; if that does not work, fill in the full pointer/mouse event
    sequence.

    Why it is needed: some V5 controls (the sampler dropdown, the gender menu after adding a
    character) are components that only listen to mousedown/pointerdown, and a plain JS
    `el.click()` is **silently ineffective** on them — no error, looks like it clicked, but
    nothing happens. Callers must always read back and verify themselves.
    """
    try:
        port.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", element)
    except Exception:  # pylint: disable=broad-except
        pass
    try:
        port.click(element)
        return True
    except Exception as error:  # pylint: disable=broad-except
        print(f"  [warn] real click failed ({type(error).__name__}); "
              f"falling back to synthetic pointer events")
    return dispatch_mouse_sequence(port, element)


def dispatch_mouse_sequence(port, element) -> bool:
    """Directly fill in pointerdown→mousedown→pointerup→mouseup→click. Used on the retry path
    for "it really was clicked but the component did not respond"."""
    try:
        port.execute_script(_MOUSE_SEQUENCE_JS, element)
        return True
    except Exception as error:  # pylint: disable=broad-except
        print(f"  [warn] synthetic click failed: {type(error).__name__}")
        return False


def find_character_name_element(port, label: str):
    """Return the "name element" representing a character card (an input on V5, a text node on
    V4.5)."""
    return port.execute_script(
        _JS_CHAR_HELPERS + "return __findCharName(arguments[0]);", label)


def character_card_area(port, label: str):
    """Return **that character card's own** prompt field; None if the card does not exist or
    is collapsed.

    Deliberately does not return `find_prompt_areas()[N]`: under V5 every character field is
    visible at the same time, and taking one by index would silently get someone else's field
    in cases such as "Character 2 was removed".
    """
    return port.execute_script(
        _JS_CHAR_HELPERS + """
        const nameEl = __findCharName(arguments[0]);
        if (!nameEl) return null;
        const card = __charCard(nameEl);
        if (!card) return null;
        for (const a of card.querySelectorAll(
               "textarea, [contenteditable='true']")) {
          if (a.offsetParent !== null) return a;
        }
        return null;
        """,
        label,
    )


def ensure_character_expanded(port, label: str) -> bool:
    """Make sure a character card is expanded (its prompt field visible); returns whether that
    succeeded.

    A collapsed V5 card does not show its prompt field, and the header row's `unfold` icon
    has to be pressed to expand it. On V4.5, clicking the heading itself toggles focus — both
    are tried, judged by "does this card's prompt field appear", not by guessing from return
    values.
    """
    if character_card_area(port, label) is not None:
        return True
    name_el = find_character_name_element(port, label)
    if name_el is None:
        return False
    icon = port.execute_script(
        _JS_CHAR_HELPERS + "return __cardIcon(arguments[0], 'unfold');",
        name_el)
    if icon is not None:
        robust_click(port, icon)
        human_pause(0.8, 1.2)
        if character_card_area(port, label) is not None:
            return True
    # V4.5 fallback: click the header row itself (there it means focus, not expand).
    row = port.execute_script(
        _JS_CHAR_HELPERS + "return __charHeaderRow(arguments[0]);", name_el)
    if row is not None:
        robust_click(port, row)
        human_pause(0.8, 1.2)
    return character_card_area(port, label) is not None


def click_add_character_control(port, gender: str = "Female",
                                timeout: float = 8.0) -> bool:
    """Press "add a character" and pick a gender. Both webrunner variants share this copy.

    V4.5 has a button labelled `Add Character`; **V5 replaced it with a 44x40 icon button in
    the "Character Prompts" header row, with no text / no aria-label / no title**, so the old
    `//button[contains(., 'Add Character')]` never finds anything. Both are recognised here:
    look for the old button first, and fall back to the header row's icon button if it is
    not found. The V5 gender menu is Female / Male / Other.
    """
    # The timeout is an **interval** → monotonic clock (see `reject_cookies` for details). A
    # wall clock jumping back turns a DOM timeout of a few seconds into a stall of hours —
    # **getting stuck is harder to diagnose than failing**, since the supervisor sees a
    # process that is alive yet doing nothing; jumping forward degrades it into a single try.
    end = time.monotonic() + timeout
    btn = None
    while time.monotonic() < end:
        for cand in port.find_elements_xpath(
                "//button[contains(., 'Add Character')]"):
            try:
                if cand.is_displayed() and cand.is_enabled():
                    btn = cand
                    break
            except Exception:  # pylint: disable=broad-except
                continue
        if btn is not None:
            break
        btn = port.execute_script(
            """
            // The "Character Prompts" header row: the smallest visible element that
            // contains both that heading text and at least one visible button. "Smallest
            // that contains a button" rather than walking up a fixed number of levels —
            // the DOM depth changes once character cards are added, and a hard-coded
            // depth would drift.
            let best = null, bestLen = Infinity;
            for (const el of document.querySelectorAll('div, span, section')) {
              if (el.offsetParent === null) continue;
              const t = (el.innerText || '').trim();
              if (t.indexOf('Character Prompts') !== 0) continue;
              let has = false;
              for (const b of el.querySelectorAll('button')) {
                if (b.offsetParent !== null) { has = true; break; }
              }
              if (!has) continue;
              const len = el.outerHTML.length;
              if (len < bestLen) { best = el; bestLen = len; }
            }
            if (!best) return null;
            const btns = [];
            for (const b of best.querySelectorAll('button')) {
              if (b.offsetParent !== null && !b.disabled) btns.push(b);
            }
            return btns.length ? btns[btns.length - 1] : null;
            """
        )
        if btn is not None:
            break
        time.sleep(0.3)
    if btn is None:
        print("  add-character control not found")
        snap(port, "no_add_character")
        return False
    robust_click(port, btn)
    human_pause(0.6, 1.0)
    if not _click_gender(port, gender, timeout=4.0):
        print(f"  WARN: gender option {gender!r} not clicked")
    human_pause(0.4, 0.8)
    return True


def count_characters(port) -> int:
    """Count how many Character N slots are currently shown.

    **Must return a real int — callers do `while have > N`.** Selenium's
    `execute_script` is typed `Any` and genuinely *can* return `None`
    (Chrome handing back a null script result when the page is
    mid-transition or the renderer is under memory pressure — OOM is the
    dominant failure mode on these boxes). A raw `None` here would flow
    straight into `ensure_two_characters` / `remove_all_character_slots`'s
    `while have > N`, raising
    `'>' not supported between instances of 'NoneType' and 'int'` — which,
    on the single-image idle serve path, aborted the whole request with a
    raw TypeError. Coerce defensively: a non-int result becomes 0
    ("couldn't count / nothing visible"), which the no-progress guards in
    both loops handle gracefully (trim becomes a no-op; the idle serve
    still clears residual character areas via `find_prompt_areas`)."""
    raw = port.execute_script(
        _JS_CHAR_HELPERS + """
        let max = 0;
        for (const pair of __charNameEls()) {
          const m = pair[1].match(/^Character (\\d+)$/);
          if (m) {
            const n = parseInt(m[1], 10);
            if (n > max) max = n;
          }
        }
        return max;
        """
    )
    try:
        return int(raw)
    except (TypeError, ValueError):
        print(f"  WARN: count_characters got non-int result {raw!r}; "
              f"treating as 0", file=sys.stderr)
        return 0


def debug_character_buttons(port, label: str) -> None:
    info = port.execute_script(
        _JS_CHAR_HELPERS + """
        const want = arguments[0];
        const header = __findCharName(want);
        if (!header) return [];
        // Walk up to a container that has many buttons (the entire character card).
        let card = header;
        for (let i = 0; i < 10 && card; i++) {
          card = card.parentElement;
          if (!card) break;
          if (card.querySelectorAll('button').length >= 5) break;
        }
        if (!card) return [];
        return Array.from(card.querySelectorAll('button')).map((b, i) => {
          const r = b.getBoundingClientRect();
          return {
            i,
            aria: b.getAttribute('aria-label'),
            title: b.getAttribute('title'),
            text: (b.innerText || '').slice(0, 30),
            disabled: b.disabled,
            x: Math.round(r.x),
            y: Math.round(r.y),
            w: Math.round(r.width),
            h: Math.round(r.height),
            html: b.outerHTML.slice(0, 200),
          };
        });
        """,
        label,
    )
    print(f"  ALL buttons in {label} card:")
    for b in info:
        print(f"    [{b['i']}] aria={b['aria']!r} title={b['title']!r} text={b['text']!r}"
              f" disabled={b['disabled']} pos=({b['x']},{b['y']},{b['w']}x{b['h']})")
        print(f"        html={b['html']!r}")


def remove_character_slot(port, label: str) -> bool:
    """Focus Character N then click its trash icon (via port.click)."""
    # Focus the character first so its trash icon is visible.
    expand_character_section(port, label)
    human_pause(0.4, 0.7)
    debug_character_buttons(port, label)
    # Find the trash button — search the whole document for aria-label / svg
    # path patterns commonly used for delete.
    btn = port.execute_script(
        _JS_CHAR_HELPERS + """
        const want = arguments[0];
        const header = __findCharName(want);
        if (!header) return null;
        // V5 fast path: the icon's CSS mask file is literally named trash.<hash>.svg, so
        // recognise it directly.
        const direct = __cardIcon(header, 'trash');
        if (direct) return direct;
        let card = header;
        for (let depth = 0; depth < 10 && card; depth++) {
          const buttons = Array.from(card.querySelectorAll('button'));
          for (const b of buttons) {
            if (b.disabled || b.offsetParent === null) continue;
            const r = b.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) continue;
            const semantic = ((b.getAttribute('aria-label') || '') + ' '
              + (b.getAttribute('title') || '')).toLowerCase();
            if (/delete|remove|trash|bin/.test(semantic)) return b;
            for (const node of [b, ...b.querySelectorAll('*')]) {
              const style = window.getComputedStyle(node);
              const icon = ((style.maskImage || '') + ' '
                + (style.webkitMaskImage || '') + ' '
                + (style.backgroundImage || '')).toLowerCase();
              if (/trash|delete|remove|bin/.test(icon)) return b;
            }
          }
          card = card.parentElement;
        }
        return null;
        """,
        label,
    )
    if not btn:
        # NovelAI icon buttons have no semantic aria/title. The card header
        # controls are ordered move-up, move-down, delete. Select the rightmost
        # visible enabled icon on that header row. The previous "last
        # descendant button" fallback could click an off-screen Position or
        # AI Choice control while reporting success.
        btn = port.execute_script(
            _JS_CHAR_HELPERS + """
            const want = arguments[0];
            const header = __findCharName(want);
            if (!header) return null;
            const hr = header.getBoundingClientRect();
            let card = header;
            for (let depth = 0; depth < 10 && card; depth++) {
              const buttons = Array.from(card.querySelectorAll('button'))
                .filter(b => {
                  if (b.disabled || b.offsetParent === null) return false;
                  const r = b.getBoundingClientRect();
                  const text = (b.innerText || '').trim();
                  return r.width > 0 && r.height > 0 && !text
                    && Math.abs((r.y + r.height / 2)
                              - (hr.y + hr.height / 2)) < 35;
                });
              // V5's header row order is up / down / check / trash / **unfold**, so the
              // rightmost button is unfold, not delete — the old "take the rightmost" would
              // click the wrong one and report success. Rule out the known non-delete icons
              // by CSS mask file name first.
              const notTrash = /directional_arrow|unfold|check/;
              const filtered = buttons.filter(b => {
                const nodes = [b].concat(Array.from(b.querySelectorAll('*')));
                for (const node of nodes) {
                  const st = window.getComputedStyle(node);
                  const mi = ((st.maskImage || '') + ' '
                            + (st.webkitMaskImage || '')).toLowerCase();
                  if (notTrash.test(mi)) return false;
                }
                return true;
              });
              const pool = filtered.length ? filtered : buttons;
              if (pool.length >= 1 && buttons.length >= 2) {
                pool.sort((a, b) =>
                  b.getBoundingClientRect().x - a.getBoundingClientRect().x);
                return pool[0];
              }
              card = card.parentElement;
            }
            return null;
            """,
            label,
        )
    if not btn:
        return False
    port.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
    port.click(btn)
    human_pause(0.4, 0.8)
    # Some UIs show a confirm dialog — click any "Confirm" / "Yes" / "Delete".
    for confirm in ("Confirm", "Yes", "Delete", "OK"):
        if _click_option_by_text(port, confirm, timeout=0.6):
            human_pause(0.3, 0.6)
            break
    return True


def set_character2_enabled(port, enabled: bool) -> bool:
    """Make the NovelAI UI contain Character 2 exactly when enabled is true.

    Empty char2 queue entries must remove the second card, not merely skip
    filling it: a still-present card can retain the preceding pair's prompt
    and leak that character into the next generation. If a later dynamic
    queue entry needs char2 again, add the card back before filling it.
    """
    have = count_characters(port)
    if enabled:
        if have >= 2:
            return True
        # The add-character control looks completely different on V4.5 / V5, so always go
        # through the shared implementation.
        if not click_add_character_control(port, "Female"):
            return False
        human_pause(0.5, 0.9)
        now = count_characters(port)
        if now < 2:
            print(f"  WARN: Add Character did not create Character 2 "
                  f"(count={now})")
            snap(port, "character2_add_failed")
            return False
        print("  added Character 2 for non-empty char2 prompt")
        return True

    if have < 2:
        return True
    if not remove_character_slot(port, "Character 2"):
        snap(port, "character2_remove_failed")
        return False
    human_pause(0.5, 0.9)
    now = count_characters(port)
    if now >= 2:
        print(f"  WARN: Character 2 still present after delete (count={now})")
        snap(port, "character2_remove_failed")
        return False
    print("  removed Character 2 because char2 prompt is empty")
    return True


def remove_all_character_slots(port) -> None:
    """One-shot helper: delete "every" character SLOT outright (target hard-coded to 0), so
    the single-image generation UI really has 0 character boxes left, only the main prompt.
    **Only called on the idle one-shot path** (in_band=False: after serving, main() returns 0
    straight away and no batch runs after it; on the in-band path, deleting boxes would keep
    _refill_character_fields from refilling them → the batch character's remaining images
    would be generated with boxes missing).
    **Correction 2026-09-05**: this used to say "silently fails because areas[1] cannot be
    found", which was the old index-based lookup. `fill_character_prompt` long ago switched
    to locating the character card first, and when no card is found it prints
    `"{label} slot not present; cannot fill"` and returns False — **not silent**. The
    conclusion is unchanged (in-band must not delete boxes), but stop looking for a silent
    failure that does not exist.

    NovelAI numbers the character boxes Character 1..N, and they can only be removed cleanly
    from "the tail" (same reason as ensure_two_characters: avoid renumbering). Purely
    best-effort: does not raise (reuses the existing removal flow; the caller then verifies by
    content).

    **NovelAI forces at least 1 character box to remain**: the last box has no trash button,
    so remove_character_slot's trash search finds nothing and falls back to clicking "the
    last button of that box" (not the delete button), which does not really delete anything
    yet still returns True; this function's no-progress guard (new_count >= have) therefore
    breaks when 1 box is left. So this function usually stops at 1 box, and that box may
    still hold leftover content — the caller (the idle one-shot serve) must, after calling
    this function, clear the remaining character boxes to empty strings (going by
    find_prompt_areas[1:], verified by content), and must not rely on count==0 alone."""
    have = count_characters(port)
    print(f"one-shot remove-all-slots: have {have}, target 0")
    # Trim from the tail (Character N, Character N-1, …) — copy
    # ensure_two_characters's no-progress guard exactly.
    safety = 0
    while have > 0 and safety < 20:
        safety += 1
        label = f"Character {have}"
        ok = remove_character_slot(port, label)
        human_pause(0.5, 0.9)
        new_count = count_characters(port)
        if not ok or new_count >= have:
            print(f"  could not remove {label} (delete clicked={ok}, count={new_count})")
            break
        have = new_count
        print(f"  removed {label}; now {have} slot(s)")


def expand_character_section(port, label: str) -> bool:
    """Make Character N's prompt field writable (present and expanded); returns True on
    success.

    On V4.5 the meaning is "focus this card" (only the focused card shows its field); on V5
    it is "expand this card" (once expanded, every card's field is visible at the same time).
    The two have the same **observable result**: this card's own prompt field can be
    obtained. So this always goes by whether `character_card_area()` can obtain it, without
    guessing which meaning applies, and without relying on fragile signals like "how many
    times was it clicked / did the state change".
    """
    return ensure_character_expanded(port, label)


def _read_textarea_value(port, element) -> str:
    return port.execute_script(
        "const e = arguments[0];"
        "return (e.tagName==='TEXTAREA' || e.tagName==='INPUT') ? e.value : (e.innerText || '');",
        element,
    )


def fill_character_prompt(port, character_index: int, text: str) -> bool:
    """Write `text` into **Character N's own** prompt field.

    **No longer uses `find_prompt_areas()[1]`.** V4.5 only shows the focused card's field, so
    index 1 happened to be it; on V5, once expanded, every character field is visible at the
    same time (`areas = [main prompt, char1, char2, …]`), and keeping index 1 would wipe
    **char1**'s content when "Character 2 was removed" — raising no error, just producing
    images with the characters mixed up, the kind of failure hardest to notice from the
    results. Changed to locate that character's card first, then take the field inside the
    card.
    """
    label = f"Character {character_index}"
    if find_character_name_element(port, label) is None:
        # The card does not exist at all (an empty todo_character2 row makes
        # set_character2_enabled remove the whole card). A request to "clear" counts as done
        # (there was no content anyway); a request to write a non-empty value returns False,
        # leaving it to the caller's retry / skip.
        print(f"  {label} slot not present; "
              + ("nothing to clear" if not text.strip() else "cannot fill"))
        return not text.strip()
    if not ensure_character_expanded(port, label):
        print(f"  {label} card present but could not be expanded")
        snap(port, f"missing_{label.lower().replace(' ', '_')}")
        return False
    el = character_card_area(port, label)
    if el is None:
        snap(port, f"missing_{label.lower().replace(' ', '_')}")
        return False
    existing = _read_textarea_value(port, el)
    # The wording must itself make clear that this is the value read **before writing**.
    # This is the generic fill record, and on the verify → refill path it appears right after
    # "the field does not match, refilling", where the meaning is exactly reversed: readers
    # take it as "the state after refilling", so an empty value looks like "still empty after
    # refilling" — which looks exactly like this project's most expensive class of failure
    # (fields knocked back, then quietly producing a whole batch of images with the wrong
    # prompt). What it actually proves is the opposite: the field really was empty before the
    # refill, so this refill had something to fix. It stays and only the wording changed,
    # because that evidence is itself useful.
    print(f"[{label}] card area before write: {existing[:60]!r}")
    port.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    try:
        el.click()
    except Exception:  # pylint: disable=broad-except
        pass
    human_pause(0.2, 0.4)
    return fill_textarea_like(port, el, text)


def verify_character_prompt(port, character_index: int, expected: str) -> bool:
    """Read the character field back before generating and confirm its content still equals
    the expected value.

    The verify at fill time cannot catch pollution "after the fill": when a leftover
    autocomplete dropdown is hit by a later coordinate click, a suggested tag gets inserted
    at the end of the prompt, and since each entry's last token differs, it looks like a
    random tag. On a mismatch it refills once; if it still does not match it only WARNs and
    carries on, without aborting (keeping the current value is more useful than interrupting
    the whole run). All 240 images share one prompt and the generate loop does not touch the
    fields, so verifying once per character batch is enough.

    Like `fill_character_prompt`, it is card-oriented — taking a field by index would compare
    against someone else's field when the number of cards changes, and after misjudging that
    as drift, the "refill" would wipe out a different character instead.

    **Logging contract: every path must state its outcome, including the successful one.**
    Whoever reads the log should not have to read the source to know "was that character
    rescued or not". Each of the three outcomes has its own line: the field was empty (note,
    steady state, see the triage comment below), the field was polluted (WARN + snapshot),
    still wrong after the refill (WARN + snapshot + returns False). A successful refill prints
    `refilled OK`.
    """
    label = f"Character {character_index}"
    expected = expected.strip()
    try:
        if find_character_name_element(port, label) is None:
            # The card does not exist (see the same guard in fill_character_prompt).
            print(f"  {label} slot not present; verify "
                  + ("satisfied (nothing to check)" if not expected
                     else "skipped (slot missing)"))
            return not expected
        if not ensure_character_expanded(port, label):
            print(f"  WARN: verify {label} — card not expandable; skipping")
            return False
        el = character_card_area(port, label)
        if el is None:
            print(f"  WARN: verify {label} — prompt area not found; skipping")
            return False
        actual = _read_textarea_value(port, el).strip()
        if actual == expected:
            return True
        # The two kinds of drift mean completely different things, so severity is split
        # **by the field's actual content**:
        #
        # * Read back empty = the write did not stay in the field. When the quota dialog will
        #   not close, the whole page is reloaded, and the refill sequence after a reload
        #   itself briefly knocks the previous character field back to blank. Measured
        #   (`WEBRunner.log` 2026-09-03 to 09-08): **all** 90 drifts were of this kind, and
        #   **all** were fixed by a single refill (`still mismatched after refill` 0 times,
        #   `char*_drift` snapshots 0), and the 90 drifts map one-to-one onto 90 reloads, with
        #   no second source. In other words it is this site's **steady state** after a
        #   reload, not an anomaly.
        #   And the "reload" itself is not an exceptional path either: after layered
        #   dismissing (rule 3) went live (from 09-07 17:17), 18 blocks were closed **0
        #   times**, all 38 corner clicks failed, and every one of them fell back to a
        #   full-page reload — so it is currently the **only path that actually happens** on
        #   this site, and in that window there were 20 drifts and likewise 0
        #   `still mismatched`. Two time windows, the same conclusion.
        #   A WARN that fires once in every normal cycle is no WARN at all — this repo has
        #   already paid for the same lesson once (95.8% of the lines in `discord_bot.log`
        #   were the same `rpc apply ->` sentence, drowning the 4% that actually had
        #   something to say), and this is its mirror image: shouting only half of the scary
        #   part, and swallowing the reassuring half instead. So it is downgraded to a note.
        # * Read back **non-empty but different** = there is other content in the field,
        #   which is the autocomplete pollution the docstring talks about (a suggested tag
        #   inserted at the end of the prompt). Measured 0 times; when it really happens it
        #   has to be visible, so it stays WARN + snapshot.
        #
        # The criterion is deliberately "what is in the field", not "which caller called":
        # a context flag passed in by the caller gets forgotten when a new call site is added
        # (the symptom is the severity being quietly mislabelled), while the content is read
        # back from the page itself and cannot drift from reality. Only the wording is
        # downgraded — detection, refill, the WARN on failure and the return value are all
        # unchanged.
        if actual:
            print(f"  WARN: {label} drifted after fill "
                  f"({len(actual)} vs {len(expected)} chars); refilling")
            snap(port, f"char{character_index}_drift")
        else:
            print(f"  note: {label} was blank after fill "
                  f"(expected {len(expected)} chars); refilling")
        fill_character_prompt(port, character_index, expected)
        human_pause(0.3, 0.6)
        el2 = character_card_area(port, label)
        if el2 is not None:
            if _read_textarea_value(port, el2).strip() == expected:
                # **Success has to speak up too.** The old version simply returned True
                # here, so the log stopped at "the field did not match, refilled" with no
                # follow-up, and people had to read the source to learn the outcome — and
                # that half-told story looks exactly like the most expensive failure. Closing
                # this loop takes one line; a silent success is asking whoever reads the log
                # to go read the code.
                print(f"  {label} refilled OK ({len(expected)} chars)")
                return True
        print(f"  WARN: {label} still mismatched after refill; "
              f"continuing with current value")
        # The snapshot moved here: what really needs the on-site picture is "even the
        # refill could not rescue it", not the blank that happens every round and fixes
        # itself every time. The non-empty branch above still snaps on its own.
        snap(port, f"char{character_index}_still_mismatched")
        return False
    except Exception as error:  # pylint: disable=broad-except
        # `_long_error`: this runs only twice per character batch (char1 / char2), so it is
        # not a repeated line; and the `element click intercepted` detail most wanted here is
        # at the tail of the message, which 180 characters would cut off.
        print(f"  WARN: verify {label} failed: {_long_error(error)}")
        return False


def _click_option_by_text(port, text: str, timeout: float = 5.0) -> bool:
    """Click the visible element whose own text node equals `text`."""
    # The timeout is an **interval** → monotonic clock (see `reject_cookies` for details). A
    # wall clock jumping back turns a DOM timeout of a few seconds into a stall of hours —
    # **getting stuck is harder to diagnose than failing**, since the supervisor sees a
    # process that is alive yet doing nothing; jumping forward degrades it into a single try.
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        candidates = port.find_elements_xpath(f"//*[normalize-space(text())='{text}']")
        for el in candidates:
            try:
                if not el.is_displayed():
                    continue
                port.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                port.click(el, pause=0.1)
                return True
            except Exception:  # pylint: disable=broad-except
                continue
        time.sleep(0.2)
    return False


def _open_resolution_dropdown(port) -> bool:
    """Click whatever resolution preset is currently selected to open the menu."""
    options = [
        f"{size} {shape}"
        for size in ("Small", "Normal", "Large", "Wallpaper")
        for shape in ("Portrait", "Landscape", "Square")
    ]
    # Find the smallest element whose own text matches a known preset.
    target = port.execute_script(
        """
        const want = arguments[0];
        let best = null, bestLen = Infinity;
        for (const el of document.querySelectorAll('div, span, button')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          if (want.includes(t)) {
            const len = el.outerHTML.length;
            if (len < bestLen) { best = el; bestLen = len; }
          }
        }
        return best;
        """,
        options,
    )
    if not target:
        return False
    port.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
    port.click(target)
    human_pause(0.5, 1.0)
    return True


def _number_input_by_aria(port, aria: str):
    """Return the visible number input whose aria-label equals `aria` (for V5's W / H)."""
    return port.execute_script(
        """
        const want = arguments[0];
        for (const el of document.querySelectorAll("input[type='number']")) {
          if (el.offsetParent === null) continue;
          if ((el.getAttribute('aria-label') || '') === want) return el;
        }
        return null;
        """,
        aria,
    )


def _read_number_input_by_aria(port, aria: str):
    el = _number_input_by_aria(port, aria)
    if el is None:
        return None
    try:
        return float(_read_textarea_value(port, el))
    except (TypeError, ValueError):
        return None


def _click_by_aria_label(port, aria: str) -> bool:
    """Click the visible element whose aria-label equals `aria` (climbing to a clickable
    ancestor when needed)."""
    el = port.execute_script(
        """
        const want = arguments[0];
        for (const el of document.querySelectorAll('[aria-label]')) {
          if (el.offsetParent === null) continue;
          if ((el.getAttribute('aria-label') || '') !== want) continue;
          let node = el;
          for (let i = 0; i < 6 && node; i++) {
            const cs = window.getComputedStyle(node);
            if (node.tagName === 'BUTTON' || node.onclick
                || cs.cursor === 'pointer'
                || node.getAttribute('role') === 'button') return node;
            node = node.parentElement;
          }
          return el.parentElement || el;
        }
        return null;
        """,
        aria,
    )
    if el is None:
        return False
    return robust_click(port, el)


def _select_resolution_v5(port, group: str, item: str) -> bool:
    """V5 resolution: a category dropdown (Normal / Large / Wallpaper / Small / Custom) + two
    number boxes whose aria-labels are `W` / `H`.

    **Orientation (Portrait / Landscape) is no longer an option on V5**, just "which of W and
    H is larger". So this is done by "pick the category → press Swap width and height when
    needed", and deliberately does **not** memorise each category's pixel values — those
    numbers change whenever the site is revised, and memorised values would silently produce
    the wrong size on the day of a revision; comparing sizes always holds. W/H are always read
    back for verification at the end.
    """
    if not _click_by_aria_label(port, "Select a Resolution Category"):
        print("  [v5] resolution category control not found")
        return False
    human_pause(0.6, 1.0)
    picked = port.execute_script(
        """
        const want = arguments[0];
        let best = null, bestLen = Infinity;
        for (const el of document.querySelectorAll(
               '[role="option"], li, div, span, button')) {
          if (el.offsetParent === null) continue;
          if ((el.innerText || '').trim() !== want) continue;
          const len = el.outerHTML.length;
          if (len < bestLen) { best = el; bestLen = len; }
        }
        return best;
        """,
        group,
    )
    if picked is None:
        print(f"  [v5] resolution category {group!r} not in the list")
        snap(port, "resolution_category_not_found")
        return False
    robust_click(port, picked)
    human_pause(0.8, 1.2)

    width = _read_number_input_by_aria(port, "W")
    height = _read_number_input_by_aria(port, "H")
    if width is None or height is None:
        print("  [v5] W/H inputs not readable")
        snap(port, "resolution_wh_missing")
        return False
    want = item.strip().lower()
    if want == "square":
        ok = width == height
        print(f"  [v5] resolution {width:.0f}x{height:.0f} square -> {ok}")
        return ok
    if ((want == "landscape" and width < height)
            or (want == "portrait" and height < width)):
        if not _click_by_aria_label(port, "Swap width and height"):
            print("  [v5] swap width/height control not found")
            snap(port, "resolution_swap_missing")
            return False
        human_pause(0.6, 1.0)
        width = _read_number_input_by_aria(port, "W")
        height = _read_number_input_by_aria(port, "H")
        if width is None or height is None:
            return False
    ok = ((want == "landscape" and width > height)
          or (want == "portrait" and height > width))
    print(f"  [v5] resolution -> {width:.0f}x{height:.0f} "
          f"({group} {item}) verified={ok}")
    if not ok:
        snap(port, "resolution_orientation_failed")
    return ok


def select_resolution(port, group: str = "Normal", item: str = "Landscape",
                      timeout: float = 10.0) -> bool:
    """Set the output resolution to `group` `item` (e.g. Normal Landscape).

    Two paths: V4.5 has a single preset dropdown like "Normal Landscape"; **V5 split it into a
    category dropdown + W / H number boxes**, and orientation became purely the size
    relationship of W/H. The old path is tried first (fast, and on V4.5 it is the originally
    verified behaviour), and only on failure does it take the V5 path.
    """
    if _select_resolution_preset(port, group, item, timeout=timeout):
        return True
    print("  preset-style resolution picker not usable; trying the V5 layout")
    return _select_resolution_v5(port, group, item)


def _select_resolution_preset(port, group: str = "Normal",
                              item: str = "Landscape",
                              timeout: float = 10.0) -> bool:
    """Open the Resolution dropdown and pick the first option matching `item`.
    The dropdown shows entries like "Portrait (832x1216)" / "Landscape (1216x832)";
    items are ordered Normal → Large, so the first match is the Normal preset."""
    if not _open_resolution_dropdown(port):
        # On V5 this is normal (there is no preset dropdown), not an error — the caller then
        # switches to `_select_resolution_v5`.
        print("  no preset-style resolution dropdown on this page")
        return False
    clicked = port.execute_script(
        """
        const item = arguments[0];
        for (const el of document.querySelectorAll('div, li, span, button')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          // Match "Landscape (...)" — the FIRST such option is Normal Landscape.
          if (t.startsWith(item + ' (')) {
            el.scrollIntoView({block:'center'});
            el.click();
            return t;
          }
        }
        return null;
        """,
        item,
    )
    if clicked:
        human_pause(0.4, 0.8)
        print(f"  resolution -> {clicked!r}")
        return True
    snap(port, "resolution_option_not_found")
    return False


def expand_advanced_settings(port) -> bool:
    """Click the rightmost icon in the Steps/Guidance/Seed/Sampler row.

    **This is a toggle button, not an "expand" button.** Measured 2026-08-22: clicking it
    again while the panel is already expanded **collapses** it (the Steps row under
    `AI Settings` disappears entirely). So **never call it unconditionally** — the panel's
    expanded state is remembered by the browser profile, so the starting state is uncertain.
    To "make sure Rescale is visible", call `ensure_rescale_visible()`, which goes by the
    result and is safe to call repeatedly.
    """
    target = port.execute_script(
        """
        // Smallest row whose text contains both Steps & Sampler.
        let row = null, rowLen = Infinity;
        for (const el of document.querySelectorAll('div')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          if (!/Steps/.test(t) || !/Sampler/.test(t) || t.length > 220) continue;
          if (t.length < rowLen) { row = el; rowLen = t.length; }
        }
        if (!row) return null;
        // Among the row's svg/button children, pick the one with max x (rightmost).
        let best = null, bestX = -Infinity;
        for (const el of row.querySelectorAll('svg, button')) {
          if (el.offsetParent === null) continue;
          const r = el.getBoundingClientRect();
          if (r.width === 0) continue;
          if (r.x > bestX) { bestX = r.x; best = el; }
        }
        return best || row;
        """
    )
    if not target:
        print("advanced settings header not found")
        return False
    port.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
    port.click(target)
    human_pause(0.8, 1.4)
    return True


def _has_variety_plus(port) -> bool:
    """Whether the page has a Variety+ control at all. V5 removed it; see
    `configure_sampler_settings` for why "absent" does not mean "failed"."""
    return bool(port.execute_script(
        """
        for (const el of document.querySelectorAll('div, span, label')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          if (t.length > 40) continue;
          if (t === 'Variety+' || t.indexOf('Variety+') === 0) return true;
        }
        return false;
        """
    ))


def set_variety_plus(port, enable: bool = True) -> bool:
    """Toggle the Variety+ switch in the advanced settings."""
    return port.execute_script(
        """
        const want = arguments[0];
        for (const el of document.querySelectorAll('div, span, label')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          if (t !== 'Variety+' && !t.startsWith('Variety+')) continue;
          if (t.length > 40) continue;
          // Search around the label for a toggle / button.
          let scope = el;
          for (let i = 0; i < 5 && scope; i++) {
            const inp = scope.querySelector(
              "input[type='checkbox'], [role='switch'], button[aria-pressed], button[aria-checked]"
            );
            if (inp && inp.offsetParent !== null) {
              const isOn = inp.getAttribute('aria-pressed') === 'true'
                        || inp.getAttribute('aria-checked') === 'true'
                        || (inp.tagName === 'INPUT' && inp.checked);
              if (isOn !== want) inp.click();
              return true;
            }
            const btn = scope.querySelector('button');
            if (btn && btn.offsetParent !== null) {
              btn.click();
              return true;
            }
            scope = scope.parentElement;
          }
        }
        return false;
        """,
        enable,
    )


def set_numeric_setting(port, label: str, value) -> bool:
    """Find a setting input by its label text and set its numeric value (React-safe)."""
    return port.execute_script(
        """
        const wantLabel = arguments[0];
        const wantVal = String(arguments[1]);

        const labelEls = [];
        for (const el of document.querySelectorAll('label, div, span')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          if (t === wantLabel || t.split('\\n')[0].trim() === wantLabel) {
            labelEls.push(el);
          }
        }
        for (const lab of labelEls) {
          let scope = lab;
          for (let i = 0; i < 5 && scope; i++) {
            const inp = scope.querySelector("input[type='number']");
            if (inp && inp.offsetParent !== null) {
              const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
              ).set;
              // First focus and clear (some controls only accept values when focused).
              inp.focus();
              setter.call(inp, '');
              inp.dispatchEvent(new Event('input', {bubbles:true}));
              setter.call(inp, wantVal);
              inp.dispatchEvent(new Event('input', {bubbles:true}));
              inp.dispatchEvent(new Event('change', {bubbles:true}));
              inp.dispatchEvent(new Event('blur', {bubbles:true}));
              return true;
            }
            scope = scope.parentElement;
          }
        }
        return false;
        """,
        label,
        value,
    )


def read_numeric_setting(port, label: str):
    return port.execute_script(
        """
        const wantLabel = arguments[0];
        for (const lab of document.querySelectorAll('label, div, span')) {
          if (lab.offsetParent === null) continue;
          const t = (lab.innerText || '').trim();
          if (t === wantLabel || t.split('\\n')[0].trim() === wantLabel) {
            let scope = lab;
            for (let i = 0; i < 5 && scope; i++) {
              const inp = scope.querySelector("input[type='number']");
              if (inp && inp.offsetParent !== null) return parseFloat(inp.value);
              scope = scope.parentElement;
            }
          }
        }
        return null;
        """,
        label,
    )


def first_present_numeric_label(port, labels):
    """Return the first candidate label that **really exists** on this layout; None if there
    is none at all.

    Purely read-only — it only reads values and writes not a single character. The criterion
    is **exactly isomorphic** to `set_numeric_setting` (the same `label, div, span` set, the
    same "climb five levels to get `input[type='number']`"), so "found by the probe" is
    equivalent to "set can find the control", not a separate way of guessing.

    Why it is needed: the colon and no-colon spellings in the candidate list **each
    correspond to one site layout**, not "old and new spellings, one of which is already
    dead". Measured (2026-08-30, 11 setups in the production log):

    * the V5 layout's labels are `Steps` / `Prompt Guidance` / `Prompt Guidance Rescale`;
    * the V4.5 layout's are `Steps:` / `Prompt Guidance:` / `Prompt Guidance Rescale:`.

    The candidate list puts the colon forms first, so on the 10 V5 runs, every numeric
    setting first let `Steps:` spin through two rounds of "write + wait + verify" for nothing
    before `Steps` got its turn, about 12 seconds in total for the three settings; the single
    V4.5 run hit `Steps:` at the first shot. **Any fixed order is bound to miss on one side**
    — swapping the order only shifts the cost to the other layout. Asking the page is the
    right approach.

    Finding nothing (all None) is not an error: the caller still tries the whole candidate
    list in order, exactly as if this function did not exist, and the worst case is only a
    few wasted pure-JS reads.
    """
    for label in labels:
        if read_numeric_setting(port, label) is not None:
            return label
    return None


def set_numeric_setting_verified(port, label, value, retries: int = 4) -> bool:
    for attempt in range(retries):
        ok = set_numeric_setting(port, label, value)
        human_pause(0.6, 1.0)
        actual = read_numeric_setting(port, label)
        if actual is not None and abs(actual - float(value)) < 0.001:
            print(f"setting {label}={value} verified (actual {actual}) on attempt {attempt + 1}")
            return True
        # The label has to be printed. The caller tries several candidate labels in turn,
        # and without saying which one, this line is an orphan in the log — in a real log two
        # lines of "failed" followed by one of "succeeded" look like the same label working
        # on and off, when in fact they are two different candidates.
        print(f"  [{label}] attempt {attempt + 1}: set returned {ok}, "
              f"actual={actual}")
        human_pause(0.5, 1.0)
    return False


def dump_advanced_labels(port) -> None:
    labels = port.execute_script(
        """
        const out = [];
        for (const lab of document.querySelectorAll('label, div, span')) {
          if (lab.offsetParent === null) continue;
          const t = (lab.innerText || '').trim();
          if (t.length === 0 || t.length > 80) continue;
          if (t.split('\\n').length > 1) continue;
          if (/Prompt|Guidance|Step|Sampler|Seed|Rescale|Decrisper|SMEA|CFG|Variety|Noise/i.test(t)) {
            out.push(t);
          }
        }
        return Array.from(new Set(out));
        """
    )
    print(f"  visible setting labels: {labels}")
    rescale = port.execute_script(
        """
        const out = [];
        for (const el of document.querySelectorAll('div, span, label')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          if (/Rescale/i.test(t) && t.length < 200) {
            out.push(t.slice(0, 100));
          }
        }
        return out.slice(0, 10);
        """
    )
    print(f"  Rescale matches: {rescale}")


def _has_rescale(port) -> bool:
    return port.execute_script(
        """
        for (const e of document.querySelectorAll('div, span, label')) {
          if (e.offsetParent === null) continue;
          const t = (e.innerText || '').trim();
          if (t.startsWith('Prompt Guidance Rescale')) return true;
        }
        return false;
        """
    )


def _find_visible_text_element(port, text: str):
    """Return the smallest visible element whose **innerText exactly equals** `text`; None if
    not found.

    The difference from `_click_option_by_text`: that one uses XPath's `text()`, which only
    matches **direct text nodes**, so it misses an element that wraps its text in a child
    node. This one matches innerText, and takes the one with the shortest `outerHTML` to
    avoid outer wrappers.
    """
    return port.execute_script(
        """
        const want = arguments[0];
        let best = null, bestLen = Infinity;
        for (const el of document.querySelectorAll(
               'div, span, label, button, [role="button"]')) {
          if (el.offsetParent === null) continue;
          if ((el.innerText || '').trim() !== want) continue;
          const len = el.outerHTML.length;
          if (len < bestLen) { best = el; bestLen = len; }
        }
        return best;
        """,
        text,
    )


def ensure_rescale_visible(port, max_attempts: int = 6) -> bool:
    """Make sure the Prompt Guidance Rescale field is visible; returns whether that succeeded.

    **Goes by the result, not by counting clicks** — the only safe way to write this
    function. Both relevant controls are **toggles**: the icon button of
    `expand_advanced_settings()`, and the "Advanced Settings" row. The panel's expanded state
    is remembered by the browser profile, so the starting state on each page visit varies,
    and any approach that "clicks once unconditionally first" **closes the panel** in half of
    the starting states, after which both targets vanish and the whole setup fails (that is
    exactly how it broke on 2026-08-22: `configure_sampler_settings` toggled once
    unconditionally on entry).

    Each round first checks whether the target is there, and acts only if it is not, trying
    both expansion paths:
      1. "Advanced Settings" is visible → click it (Rescale hides underneath it);
      2. not visible → the panel may be a collapsed compact row, so click
         `expand_advanced_settings()`'s icon to open the AI Settings panel, and the next
         round naturally reaches (1).
    """
    for attempt in range(1, max_attempts + 1):
        if _has_rescale(port):
            if attempt > 1:
                print(f"  Rescale visible after {attempt - 1} step(s)")
            return True
        advanced = _find_visible_text_element(port, "Advanced Settings")
        if advanced is not None:
            robust_click(port, advanced)
            human_pause(0.9, 1.4)
            continue
        if expand_advanced_settings(port) is False:
            print(f"  neither Rescale nor the Advanced Settings row is "
                  f"reachable (attempt {attempt})")
            break
        human_pause(0.9, 1.4)
    ok = _has_rescale(port)
    if not ok:
        snap(port, "rescale_not_visible")
    return ok


# The sampler target value. Like Steps / Guidance it is hard-coded in the shared module —
# both webrunner variants share the same set of generation parameters; only the model label
# is a per-variant constant (because it occasionally has to follow site revisions).
TARGET_SAMPLER = "Euler Ancestral"

# The known labels of the sampler dropdown. Their purpose is "recognising which one is
# currently selected": the dropdown's trigger element itself displays the current value, and
# the site provides no stable aria-label to grab, so the element can only be recognised the
# other way round, by its value. When the site adds a sampler, just add the string here; the
# logic stays unchanged.
_KNOWN_SAMPLERS = (
    "Euler Ancestral", "Euler", "DPM++ 2M SDE", "DPM++ 2M", "DPM++ SDE",
    "DPM++ 2S Ancestral", "DPM2 Ancestral", "DPM2", "DPM Fast", "DDIM V3",
    "DDIM", "k_euler_ancestral", "k_euler", "k_dpmpp_2m_sde", "k_dpmpp_2m",
    "k_dpmpp_sde", "k_dpmpp_2s_ancestral", "k_dpm_2", "k_dpm_fast", "ddim_v3",
)


def read_current_sampler(port):
    """Return the currently displayed sampler label; None if it cannot be recognised (not an
    error, see `select_sampler`)."""
    return port.execute_script(
        """
        const known = arguments[0];
        const pick = (root) => {
          let best = null, bestLen = Infinity;
          for (const el of root.querySelectorAll('div, span, button')) {
            if (el.offsetParent === null) continue;
            const t = (el.innerText || '').trim();
            if (known.indexOf(t) === -1) continue;
            const len = el.outerHTML.length;
            if (len < bestLen) { best = t; bestLen = len; }
          }
          return best;
        };
        for (const lab of document.querySelectorAll('label, div, span')) {
          if (lab.offsetParent === null) continue;
          const t = (lab.innerText || '').trim();
          if (t !== 'Sampler' && t !== 'Sampler:') continue;
          let scope = lab;
          for (let i = 0; i < 5 && scope; i++) {
            const found = pick(scope);
            if (found) return found;
            scope = scope.parentElement;
          }
        }
        return pick(document.body);
        """,
        list(_KNOWN_SAMPLERS),
    )


def _find_sampler_trigger(port):
    """Find the element that "opens the sampler dropdown when clicked".

    Three stages, each looser than the last: (1) the smallest element near the `Sampler`
    label that displays a known sampler label; (2) the smallest element on the whole page
    with a known sampler label (the fallback for when the site renames the label); (3) the
    first clickable element at the `Sampler` label's level (the last resort for when the
    current value is not in `_KNOWN_SAMPLERS`)."""
    return port.execute_script(
        """
        const known = arguments[0];
        const labels = [];
        for (const el of document.querySelectorAll('label, div, span')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          if (t === 'Sampler' || t === 'Sampler:') labels.push(el);
        }
        const pick = (root) => {
          let best = null, bestLen = Infinity;
          for (const el of root.querySelectorAll('div, span, button')) {
            if (el.offsetParent === null) continue;
            const t = (el.innerText || '').trim();
            if (known.indexOf(t) === -1) continue;
            const len = el.outerHTML.length;
            if (len < bestLen) { best = el; bestLen = len; }
          }
          return best;
        };
        for (const lab of labels) {
          let scope = lab;
          for (let i = 0; i < 5 && scope; i++) {
            const found = pick(scope);
            if (found) return found;
            scope = scope.parentElement;
          }
        }
        const anywhere = pick(document.body);
        if (anywhere) return anywhere;
        for (const lab of labels) {
          let scope = lab.parentElement;
          for (let i = 0; i < 4 && scope; i++) {
            const btn = scope.querySelector(
              "button, [role='button'], [role='combobox'], select"
            );
            if (btn && btn.offsetParent !== null) return btn;
            scope = scope.parentElement;
          }
        }
        return null;
        """,
        list(_KNOWN_SAMPLERS),
    )


def _dump_sampler_options(port) -> None:
    """Diagnostic: print the sampler labels currently visible into the log. For the same
    reason as `_dump_model_options`, when the site changes the labels this is the only
    evidence that directly shows what `TARGET_SAMPLER` should be changed to."""
    try:
        names = port.execute_script(
            """
            const out = [];
            for (const el of document.querySelectorAll('[role="option"], li, [role="listitem"], div, span, button')) {
              if (el.offsetParent === null) continue;
              const t = (el.innerText || '').split('\\n')[0].trim();
              if (!t || t.length > 40) continue;
              if (!/euler|dpm|ddim|ancestral|heun|lms|karras|sde|native/i.test(t)) continue;
              out.push(t);
            }
            return Array.from(new Set(out)).slice(0, 30);
            """
        )
    except Exception as error:  # pylint: disable=broad-except
        print(f"  [warn] could not dump sampler options: {type(error).__name__}")
        return
    print(f"  visible sampler options: {names}")


def _find_sampler_option(port, target: str):
    """Return the option element in the dropdown whose text **exactly equals** `target`; None
    if not found.

    Two deliberate choices:

    * Matches **the whole innerText exactly**, not "the first line is equal". V5 splits the
      menu into two groups, `RECOMMENDED` / `OTHER`, and a group container's innerText is
      `'OTHER\\nEuler\\nDPM++ 2S Ancestral\\n…'` — matching on the first line does filter out
      the group containers, but not an outer wrapper that "happens to wrap just one option",
      and clicking a wrapper does nothing.
    * `[role="option"]` first, then ordinary elements; within one pass the one with the
      **shortest** `outerHTML` wins, because the shortest is closest to the option itself,
      and an outer wrapper is always longer.
    """
    return port.execute_script(
        """
        const want = arguments[0];
        const pref = Array.from(document.querySelectorAll('[role="option"]'));
        const rest = Array.from(
          document.querySelectorAll('li, [role="listitem"], div, span, button'));
        for (const pool of [pref, rest]) {
          let best = null, bestLen = Infinity;
          for (const el of pool) {
            if (el.offsetParent === null) continue;
            if ((el.innerText || '').trim() !== want) continue;
            const len = el.outerHTML.length;
            if (len < bestLen) { best = el; bestLen = len; }
          }
          if (best) return best;
        }
        return null;
        """,
        target,
    )


def select_sampler(port, target: str = TARGET_SAMPLER,
                   timeout: float = 10.0) -> bool:
    """Switch the sampler to `target` and **read it back to verify**; returns True only once
    verified.

    Two details worth remembering:

    1. Matching is **exact equality**, not `startsWith` / `includes` — in the menu
       `Euler Ancestral` and `Euler` are two different samplers whose names are prefixes of
       each other, and any loose matching would pick the wrong one between them. And picking
       the wrong one **still produces images**, just with the wrong sampling, which is nearly
       impossible to tell from the results, so a failed match (which dumps the options +
       snaps) is preferred over loosening it.
    2. When it is already the target value, return True directly without opening the
       dropdown: the dropdown's trigger element displays the current value, and clicking the
       same label after opening it just closes the menu — a wasted trip that might also hit
       another control. The current `TARGET_SAMPLER` (`Euler Ancestral`) **happens to be the
       web page's own default**, so normally every setup takes this shortcut and the log
       prints `sampler already 'Euler Ancestral'`. **That does not mean the switching logic
       is not working** — `.chrome_profile/` remembers the value the user last picked, and
       the site may change its default, at which point the full click + read-back
       verification path is taken.

    Option not found / verification failed both leave evidence via
    `_dump_sampler_options()` + `snap()`.
    """
    current = read_current_sampler(port)
    if current == target:
        print(f"  sampler already {target!r}")
        return True
    trigger = _find_sampler_trigger(port)
    if not trigger:
        print("  could not locate the sampler dropdown")
        _dump_sampler_options(port)
        snap(port, "no_sampler_selector")
        return False
    port.execute_script("arguments[0].scrollIntoView({block:'center'});", trigger)
    port.click(trigger)
    human_pause(0.6, 1.1)
    # The timeout is an **interval** → monotonic clock (see `reject_cookies` for details). A
    # wall clock jumping back turns a DOM timeout of a few seconds into a stall of hours —
    # **getting stuck is harder to diagnose than failing**, since the supervisor sees a
    # process that is alive yet doing nothing; jumping forward degrades it into a single try.
    end = time.monotonic() + timeout
    option = None
    while time.monotonic() < end:
        option = _find_sampler_option(port, target)
        if option is not None:
            break
        time.sleep(0.3)
    if option is None:
        print(f"  sampler option {target!r} not found")
        _dump_sampler_options(port)
        snap(port, "sampler_option_not_found")
        return False
    # A real mouse click first. **Do not** use JS's `el.click()`: measured on 2026-08-21,
    # doing that left the menu open and the sampler completely unchanged, because this
    # dropdown is a combobox component (the page has an input with
    # `aria-label="Select a sampler"`) that listens to mousedown rather than click. A JS click
    # only sends click, so the component never receives it → silently ineffective.
    robust_click(port, option)
    human_pause(0.6, 1.0)
    if read_current_sampler(port) != target:
        # The real click did not take effect (the component may listen only to pointerdown):
        # fill in the full event sequence and try once more.
        dispatch_mouse_sequence(port, option)
        human_pause(0.6, 1.0)
    actual = read_current_sampler(port)
    if actual == target:
        print(f"  sampler -> {actual!r} verified")
        return True
    print(f"  sampler verify failed: wanted {target!r}, got {actual!r}")
    _dump_sampler_options(port)
    snap(port, "sampler_verify_failed")
    return False


def configure_sampler_settings(port) -> bool:
    # Hand it straight to `ensure_rescale_visible()`: it goes by the result, tries both
    # expansion paths, and is safe to call repeatedly. **Do not** call
    # `expand_advanced_settings()` unconditionally here first — it is a toggle, and in the
    # "panel already open" starting state it would collapse it instead (the actual failure on
    # 2026-08-22).
    all_ok = with_retry("ensure_rescale_visible",
                        lambda: ensure_rescale_visible(port),
                        max_attempts=3, sleep_range=(1, 2))
    dump_advanced_labels(port)
    # Switch the sampler first, then set the numbers. The order is intentional: when the
    # sampler changes, the site may reset Steps / Guidance to that sampler's defaults, and
    # doing it the other way round would quietly wipe the values just set and verified.
    # A failure ranks the same as the numeric settings (folded into `all_ok` together, which
    # makes `_setup_session` return False as a whole); it is not optional decoration —
    # images made with the wrong sampler look "about the same", which makes it the hardest
    # to notice.
    sampler_ok = select_sampler(port, TARGET_SAMPLER)
    print(f"setting Sampler={TARGET_SAMPLER} final -> {sampler_ok}")
    all_ok = sampler_ok and all_ok
    human_pause(0.5, 1.0)
    # Each group is "the label spellings of the same setting under different site layouts".
    # This used to say "the collapsed and expanded views have different labels" — **that
    # claim was wrong**, and the production log disproved it: whether there is a colon has
    # nothing to do with the panel being expanded, and everything to do with the **model
    # layout**.
    #     V4.5: 'Steps:', 'Prompt Guidance:', 'Prompt Guidance Rescale:'
    #     V5  : 'Steps',  'Prompt Guidance',  'Prompt Guidance Rescale'
    # (Read straight off the `visible setting labels:` of 11 setups on 2026-08-30.)
    # Both spellings are still alive, so neither can be deleted; the order is left to
    # `first_present_numeric_label` asking the page on the spot, not fixed here.
    setting_targets = [
        (("Steps:", "Steps"), 23),
        (("Prompt Guidance:", "Prompt Guidance", "Guidance:", "Guidance"), 6),
        (("Prompt Guidance Rescale:", "Prompt Guidance Rescale",
          "Guidance Rescale:", "Guidance Rescale", "Rescale:", "Rescale"), 0),
    ]
    for labels, value in setting_targets:
        # First ask the page "which candidate really exists", then start writing. The
        # candidate list mixes the label spellings of two site layouts, and any fixed order
        # is bound to spin for nothing on one side every time — for the reasoning and the
        # measurements see `first_present_numeric_label`. If the probe finds nothing, try
        # them all in the original order.
        ordered = list(labels)
        present = first_present_numeric_label(port, labels)
        if present is not None and present != ordered[0]:
            print(f"  this layout's label is {present!r} (not {ordered[0]!r}); "
                  f"skipping the candidates that do not exist")
            ordered.remove(present)
            ordered.insert(0, present)
        ok = False
        used = None
        for label in ordered:
            if set_numeric_setting_verified(port, label, value, retries=2):
                ok, used = True, label
                break
        # Report **the candidate that really succeeded**, not the first candidate. This used
        # to print `labels[0]`, so on every start the production log wrote "setting
        # Steps:=23 final -> True" — while `Steps:` in fact failed every time and it was
        # `Steps` that succeeded. A result-based verdict paired with an attempt-based
        # narrative leads whoever reads the log to the opposite conclusion. On failure, all
        # the candidates are listed.
        print(f"setting {used or ' / '.join(labels)}={value} final -> {ok}")
        all_ok = ok and all_ok
        human_pause(0.5, 1.0)
    # Variety+: V4.5 has this switch, **V5 removed it entirely** (confirmed on the real site
    # on 2026-08-21: after expanding Advanced Settings the label is nowhere in the DOM). A
    # missing control must never be treated as a failure — `configure_sampler_settings`
    # returning False makes `_setup_session` return False as a whole, and the supervisor would
    # respawn the browser endlessly.
    # Only "present but will not toggle" counts as a real failure.
    if _has_variety_plus(port):
        variety_ok = set_variety_plus(port, enable=True)
        print("Variety+ -> ON" if variety_ok
              else "WARN: could not toggle Variety+")
    else:
        variety_ok = True
        print("Variety+ control not present (removed in V5); skipping")
    return variety_ok and all_ok


def find_generate_button(port):
    """Locate the Generate button. Returns None on chromedriver transport
    stall (symmetric with `get_main_image_src` / `download_image`) — caller
    `click_generate` already polls for ≤15s and `generate_one_image`
    retries 4× on top, so a transient hiccup costs a retry instead of
    crashing the whole webrunner mid-batch (see `port.TRANSPORT_ERRORS`
    doc)."""
    try:
        return port.execute_script(
            """
            const btns = Array.from(document.querySelectorAll('button'));
            for (const b of btns) {
              if (b.offsetParent === null) continue;
              const t = (b.innerText || '').trim();
              if (/^Generate( \\d+ Image)?$/i.test(t)) return b;
            }
            for (const b of btns) {
              if (b.offsetParent === null) continue;
              if ((b.innerText || '').includes('Generate')) return b;
            }
            return null;
            """
        )
    except port.TRANSPORT_ERRORS as error:
        # A hiccup → log it and return the sentinel as before; the session is dead → raise
        # BrowserGoneError.
        _note_transport_error("find_generate_button", error)
        return None


def get_main_image_src(port) -> str | None:
    """Return the src of the most recently generated image.

    NovelAI renders newly generated images via blob: / data: URLs. The example
    gallery on the right uses CDN URLs (https://...), so we ignore those.

    Returns None on chromedriver transport stalls (default 120s HTTP timeout
    to chromedriver) — bubbling the raw urllib3.ReadTimeoutError up would
    crash the whole webrunner mid-batch. The caller (`wait_for_new_image`)
    already handles None by polling again; `generate_one_image` retries 4×
    on top of that, then `generate_loop` moves on with `consecutive_fails`.
    """
    try:
        return port.execute_script(
            """
            const imgs = Array.from(document.querySelectorAll('img'));
            let best = null, bestArea = 0;
            for (const img of imgs) {
              if (img.offsetParent === null) continue;
              if (!img.src) continue;
              // Only count freshly-generated images (blob/data URLs).
              if (!/^(blob:|data:)/.test(img.src)) continue;
              const w = img.naturalWidth || img.offsetWidth;
              const h = img.naturalHeight || img.offsetHeight;
              if (w < 200 || h < 200) continue;
              const area = w * h;
              if (area > bestArea) { best = img; bestArea = area; }
            }
            return best ? best.src : null;
            """
        )
    except port.TRANSPORT_ERRORS as error:
        # A hiccup → log it and return the sentinel as before; the session is dead → raise
        # BrowserGoneError.
        _note_transport_error("get_main_image_src", error)
        return None


def get_generation_error(port) -> dict | None:
    """Return a visible NovelAI generation error/toast, if one exists."""
    try:
        return port.execute_script(
            # A toast is a **container**, so it uses the strict `onScreen` (toasts are
            # almost always position:fixed, and `offsetParent` is always null for fixed).
            #
            # ⚠️ **The predicate is concatenated straight from `_JS_VISIBLE`; do not copy it
            # here again.** Embedded JS can reach module-level constants just the same — five
            # other constants in this file are concatenated exactly that way, and `+`-ing a
            # string inside a function is no different from doing it at module level. And the
            # cost of a copy is not just "remember to change both sides": JS function
            # declarations are hoisted and the later declaration wins, so a same-named
            # `function visible` would **override the constant's own version**, and even the
            # calls inside `onScreen` would end up using the copy.
            _JS_VISIBLE + r"""
            const selectors = [
              '[role="alert"]', '[aria-live="assertive"]',
              '[aria-live="polite"]', '[class*="toast"]',
              '[class*="Toast"]', '[class*="notification"]',
              '[class*="Notification"]'
            ];
            const nodes = Array.from(document.querySelectorAll(
              selectors.join(',')));
            window.__jeGenerationErrorRecords ||= new WeakMap();
            window.__jeGenerationErrorNextId ||= 1;
            const errorPattern = /(?:failed to generate|generation failed|error generating|an error occurred|server error|request failed|unable to generate|try again)/i;
            for (const node of nodes) {
              if (!onScreen(node)) continue;
              const text = (node.innerText || node.textContent || '')
                .replace(/\s+/g, ' ').trim();
              if (text && errorPattern.test(text)) {
                let record = window.__jeGenerationErrorRecords.get(node);
                if (!record) {
                  record = {
                    id: window.__jeGenerationErrorNextId++,
                    version: 0
                  };
                  const observer = new MutationObserver(() => {
                    record.version += 1;
                  });
                  observer.observe(node, {
                    attributes: true,
                    childList: true,
                    characterData: true,
                    subtree: true
                  });
                  window.__jeGenerationErrorRecords.set(node, record);
                }
                return {
                  id: record.id,
                  version: record.version,
                  text: text.slice(0, 300)
                };
              }
            }
            return null;
            """
        )
    except port.TRANSPORT_ERRORS as error:
        # A hiccup → log it and return the sentinel as before; the session is dead → raise
        # BrowserGoneError.
        _note_transport_error("generation-error check", error)
        return None


def wait_for_new_image(port, previous_src: str | None,
                       timeout: float = 120.0, *,
                       baseline_error: dict | None = None,
                       seen_srcs: Container[str] = ()) -> str | None:
    """Wait until an image that "has not been saved yet" appears in the main image area.

    `seen_srcs` is the set of srcs already accepted this round. **Without it this function
    would take an old image the site swapped back in for a new one**: the only criterion is
    "different from `previous_src`", and `get_main_image_src` picks "the largest visible
    blob:/data: image" — the area uses `naturalWidth`, and a thumbnail's naturalWidth is as
    large as the original's, so the history area's thumbnails tie with the main image; once
    the DOM order changes, the pick switches to another old image. An old image's blob: URL is
    alive within the same document and identical to when it was saved, so "a src that was
    already saved never counts as a new image" blocks exactly that path.

    Measured, not inferred: in the 2026-08-24 to 08-27 log, **92 of 402 generations "got a new
    image" within 1 second** — the stability check below alone takes 0.6 seconds and the
    generation itself takes 4-7 seconds, so 1 second means the first poll hit, i.e. it never
    waited at all. One of them left hard proof: image 57 and image 43 were byte-for-byte
    identical files.

    **This function raises `GenerationBlockedError`** (it does not just return None): during
    the wait it periodically probes for the purchase / plan dialog, and on a hit it shuts this
    path down outright, see `BLOCK_PROBE_INTERVAL_SEC` / `BLOCK_PROBE_GRACE_SEC`. Both call
    paths already catch this exception — the batch goes through `generate_loop`'s quota wait,
    a single image through `serve_single_image_request`'s wrap-up report.
    """
    # These two deadlines use the **monotonic** clock. Six other timeout loops of the same
    # shape in this module deliberately stay on the wall clock; this one is the exception,
    # because the consequence of the wall clock jumping back is **of a different class** here
    # than elsewhere: `next_probe` is a deadline too, and a jump back of Δ would **stall the
    # quota-dialog probe for a full Δ**, while `end` would not arrive either — so it is
    # blocked with nobody watching, bringing back untouched exactly the pathology
    # `BLOCK_PROBE_*` was introduced to fix (measured: 65 idle waits totalling 3 hours 16
    # minutes). For the other six, a jump back only means "waiting Δ longer".
    # This is also the loop that occupies the most time in the whole module (every image goes
    # through it, with a production timeout of 180 seconds), so a clock jump is most likely to
    # land inside it.
    end = time.monotonic() + timeout
    last = previous_src
    reported: set[str] = set()
    next_probe = time.monotonic() + BLOCK_PROBE_GRACE_SEC
    while time.monotonic() < end:
        cur = get_main_image_src(port)
        if cur and cur != previous_src:
            if cur in seen_srcs:
                # This log line is the only observation point for "the site swapped an old
                # image back in". Without it, this bug shows up in the log only as "this image
                # was generated especially fast", which nobody would notice — in fact it
                # quietly ran for days. Complain only once per src, so the log is not flooded.
                if cur not in reported:
                    reported.add(cur)
                    print("    the site is showing an image we already "
                          "saved; ignoring it and waiting for a new one")
            else:
                # Ensure it stays stable for >0.6s (avoid grabbing mid-load).
                time.sleep(0.6)
                cur2 = get_main_image_src(port)
                if cur2 == cur:
                    return cur
        error_text = get_generation_error(port)
        if error_text and error_text != baseline_error:
            detail = error_text.get("text", "generation failed")
            print(f"    generation failure detected in UI: {detail}")
            # The error toast itself may be "quota used up". Returning None makes the caller
            # retry, which is wasted effort for this kind of failure, so triage first: if
            # blocked, raise, and wrap up without respawning.
            _abort_if_generation_blocked(port, "a generation failure toast")
            return None
        # A purchase / plan dialog popping up means this image is not coming, and waiting
        # longer is just spinning. For the reasoning and the measured basis of the two time
        # constants, see `BLOCK_PROBE_INTERVAL_SEC` / `_GRACE_SEC`.
        # Both call paths already catch the `GenerationBlockedError` raised from here: the
        # batch goes through `generate_loop`'s quota wait, a single image through
        # `serve_single_image_request`'s wrap-up report — this only makes the same decision
        # happen 3 minutes earlier.
        now = time.monotonic()
        if now >= next_probe:
            next_probe = now + BLOCK_PROBE_INTERVAL_SEC
            _abort_if_generation_blocked(port, "waiting for a new image")
        last = cur
        time.sleep(0.5)
    print(f"timed out waiting for new image (last src={last})")
    return None


def download_image(port, src: str, save_path: Path) -> bool:
    """Fetch the image in-page, convert to base64, write to disk.

    Returns False on chromedriver transport stalls — symmetric with
    `get_main_image_src`. `download_image_with_retry` already retries 3×
    on False, so a transient chromedriver hiccup costs ≤ ~7s + retry wait
    instead of crashing the whole webrunner.
    """
    try:
        data_url = port.execute_async_script(
            """
            const src = arguments[0];
            const done = arguments[arguments.length - 1];
            (async () => {
              try {
                const res = await fetch(src);
                const blob = await res.blob();
                const r = new FileReader();
                r.onload = () => done(r.result);
                r.onerror = () => done(null);
                r.readAsDataURL(blob);
              } catch (e) { done(null); }
            })();
            """,
            src,
        )
    except port.TRANSPORT_ERRORS as error:
        # A hiccup → log it and return the sentinel as before; the session is dead → raise
        # BrowserGoneError.
        _note_transport_error("download_image", error)
        return False
    if not data_url or "," not in data_url:
        print(f"download failed for {src}")
        return False
    _header, b64 = data_url.split(",", 1)
    # A base64 decode / file write failure (a truncated data URL, a full disk, a file name
    # blocked by AV…) used to blow straight through download_image_with_retry /
    # generate_loop, ending the whole run with a critical_error; it now returns False and is
    # left to the existing 3 download retries + the consecutive_fail mechanism (symmetric
    # with how transport errors are handled).
    try:
        payload = base64.b64decode(b64)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(payload)
    except (ValueError, OSError) as error:
        print(f"  [warn] download_image decode/write failed: "
              f"{type(error).__name__}: {error}")
        return False
    return True


# `_serving_beat_within_sec` uses these two timeouts to compute the promise of the
# single-image "being served" signal, so they must be named constants, and the very copy
# `generate_one_image` really uses — written back as literals, the promise would quietly
# diverge from the actual wait length. `test_webrunner_shared` pins this down with an AST.
GENERATE_CLICK_TIMEOUT_SEC = 15.0
GENERATE_WAIT_TIMEOUT_SEC = 180.0


def click_generate(port, timeout: float = GENERATE_CLICK_TIMEOUT_SEC) -> bool:
    """Hot-path click. Wraps the whole body in `port.TRANSPORT_ERRORS`
    because the inner `scrollIntoView` / `click_via_js` calls go through
    chromedriver HTTP — if Chrome is hung, those stall selenium's 120s
    transport timeout and raw-bubble `urllib3.ReadTimeoutError`, which
    would crash the webrunner mid-batch. Return False on stall; caller
    `generate_one_image` already retries 4× with backoff."""
    # The timeout is an **interval** → monotonic clock (see `reject_cookies` for details). A
    # wall clock jumping back turns a DOM timeout of a few seconds into a stall of hours —
    # **getting stuck is harder to diagnose than failing**, since the supervisor sees a
    # process that is alive yet doing nothing; jumping forward degrades it into a single try.
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        btn = find_generate_button(port)
        if btn:
            try:
                port.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", btn)
                human_pause(0.2, 0.5)
                try:
                    btn.click()
                except port.TRANSPORT_ERRORS:
                    click_via_js(port, btn)
            except port.TRANSPORT_ERRORS as error:
                # A hiccup → return False and go through the retry; the session is dead →
                # BrowserGoneError.
                _note_transport_error("click_generate", error)
                return False
            return True
        time.sleep(0.3)
    return False


def generate_one_image(port, previous_src: str | None,
                       *, max_retries: int,
                       retry_delay: tuple[float, float],
                       seen_srcs: Container[str] = (),
                       on_attempt=None) -> str | None:
    """Click Generate and wait for a NEW image. If the image src is identical
    before and after (=generation didn't happen / failed silently), sleep
    `retry_delay` seconds and retry up to `max_retries` times. Both params
    come from `batch_config.json` via `generate_loop`.

    `seen_srcs` (optional) is passed down to `wait_for_new_image`; see the docstring there.
    The single-image path does not pass it, because it produces only one image and has
    nothing "already saved".

    `on_attempt` (optional) is called once with the attempt number at the start of **every**
    attempt. Only the single-image serve path passes it (to emit the "being served" signal,
    see `_emit_serving_beat`); the batch does not, and its behaviour is completely unchanged.
    It goes at the start of each attempt rather than once on entering the function: one serve
    runs at most `generate_max_retries` attempts (4 by default, about 225 seconds each), far
    more in total than the bot's original 600-second TTL, and emitting only once at the start
    would leave more than ten minutes of silence in between."""
    for attempt in range(1, max_retries + 1):
        if on_attempt is not None:
            on_attempt(attempt)
        print(f"    [generate attempt {attempt}/{max_retries}]")
        baseline_error = get_generation_error(port)
        # The baseline must be "what is on screen at the moment of pressing", not "what it was
        # when the previous image finished". The caller's `previous_src` is the previous
        # image's result, separated by a 20-30 second between-image wait and a DOM request
        # poll; on the quota-refill path there is also a `port.refresh()` in between, and a
        # blob: URL is bound to its document, so after a reload the old one is void — using
        # it as the baseline amounts to declaring "anything on screen counts as a new image".
        # If nothing can be read, fall back to the caller's value: during a transport stall
        # `get_main_image_src` returns None, and using None as the baseline is what is
        # really dangerous.
        displayed = get_main_image_src(port)
        baseline_src = displayed or previous_src
        if not click_generate(port):
            # The most common reason for "the Generate button cannot be found" is that the
            # whole window is already gone. Probe once (one JS round-trip), and if confirmed,
            # wrap up at once — otherwise the je variant (whose wrapper swallows exceptions
            # and always returns None) would burn all 4 retries along with the 25-30s waits in
            # between before generate_loop's counter slowly climbs.
            _abort_if_browser_gone(port, "locating the Generate button")
            # The second common reason: a purchase / plan dialog covers the whole page, so the
            # button is still there but cannot be clicked.
            _abort_if_generation_blocked(port, "locating the Generate button")
            print(f"    [generate retry {attempt}/{max_retries}] button not found")
            if attempt < max_retries:
                time.sleep(random.uniform(*retry_delay))
            continue
        new_src = wait_for_new_image(
            port, baseline_src, timeout=GENERATE_WAIT_TIMEOUT_SEC,
            baseline_error=baseline_error, seen_srcs=seen_srcs)
        if new_src and new_src != previous_src:
            return new_src
        # Not getting a new image has two possible causes: the site produced nothing this
        # time (worth retrying), or the window is already closed (retrying is pointless).
        # Probe once to tell them apart.
        _abort_if_browser_gone(port, "waiting for a new image")
        _abort_if_generation_blocked(port, "waiting for a new image")
        if attempt < max_retries:
            delay = random.uniform(*retry_delay)
            print(f"    [generate retry {attempt}/{max_retries}] failed; "
                  f"sleeping {delay:.1f}s before next attempt")
            time.sleep(delay)
        else:
            print(f"    [generate retry {attempt}/{max_retries}] failed; "
                  "no attempts remaining")
    return None


def _file_digest(path: Path) -> str | None:
    """A content hash of the saved image; None if it cannot be read.

    This is the **content-level** line of defence, complementing the **URL-level** defence
    of `seen_srcs`: the URL level blocks "the site serving an old image's blob URL again",
    but cannot block "the site minting a new blob URL for the same old image". Compare the
    content and there is nowhere to hide.

    Purely diagnostic, so read failures are always swallowed — a side channel must not bring
    image generation down.
    """
    try:
        return hashlib.blake2b(path.read_bytes(), digest_size=16).hexdigest()
    except OSError:
        return None


def download_image_with_retry(port, src: str, save_path: Path,
                              *, max_retries: int) -> bool:
    for attempt in range(1, max_retries + 1):
        if download_image(port, src, save_path):
            return True
        print(f"    [download retry {attempt}/{max_retries}] failed")
        if attempt < max_retries:
            time.sleep(random.uniform(1.0, 2.5))
    return False


# ---------- single-image (one-shot) serve path (P6 C4 — port-based) ----------
# In-band (a batch is mid-flight) OR idle one-shot (startup): the bot writes
# single_image_request.json; this serves exactly ONE image and emits exactly
# ONE `single_image_done` event per request_id. Lifted verbatim (logic-
# identical) from both variants; all DOM goes through the injected `port`.


# The margin in the promise of the single-image "being served" signal
# (`single_image_serving`). Besides the two timeouts for the button and the image, an attempt
# also makes a few JS probes (error toast, liveness probe, purchase-dialog detection), under
# ten seconds in total when healthy. chromedriver stalls are not covered by the guarantee; see
# `_serving_beat_within_sec`.
_SERVING_BEAT_MARGIN_SEC = 60.0


def _serving_beat_within_sec(batch_cfg: dict) -> float:
    """The promise of "how long until the next being-served signal arrives at the latest", in
    seconds.

    = the Generate click timeout + the new-image wait timeout + the **currently effective**
    upper bound of the retry delay + a margin. The longest stretch between two signals is one
    complete attempt (`generate_one_image` emits one at the start of every attempt), so the
    promise is computed from these quantities. 285 seconds under the default config.

    **It is computed on this side and sent with the event; the bot must not keep its own
    copy.** The first two are constants of this module and the third is a
    `batch_config.json` value; if the bot side wrote its own "a serve takes at most N
    seconds", that would be a copy that quietly diverges — someone raises the retry delay and
    the bot starts cancelling requests that are being served, which is exactly the symptom
    this signal exists to eliminate (the image is produced and nobody collects it). Same
    reason as the `quota_wait` event carrying `next_retry_sec`: keep **the value in effect at
    the time** in the event stream.

    Only the healthy path is guaranteed. During a chromedriver stall a single JS call can take
    up to 120 seconds, a few of which stacked up exceed the promise, and the bot may give up
    early on a request that would in the end have completed. That is an already-sick path; to
    cover it the promise would make "the server died" detection wait several times longer in
    the healthy case.
    """
    delay_hi = max(float(v) for v in batch_cfg["generate_retry_delay_sec"])
    return (GENERATE_CLICK_TIMEOUT_SEC + GENERATE_WAIT_TIMEOUT_SEC + delay_hi
            + _SERVING_BEAT_MARGIN_SEC)


def _emit_serving_beat(request_id: str, in_band: bool, phase: str,
                       batch_cfg: dict) -> None:
    """Emit a `single_image_serving`: "this request is being served, and the server is still
    alive".

    The bot relies on it to tell "being served, just slow" from "the server has already died".
    Before this, the background program emitted nothing between the request being written to
    disk and `single_image_done`, so the bot could only measure "how long since it was sent",
    which is the wrong measure for in-band serving.

    Timing (each one comes **before** the same request's `single_image_done`, and never after
    it): `start` = past both validations, before touching the browser; `generate` = at the
    start of every Generate attempt; `download` = when the download starts. Requests that
    fail validation (empty prompt, unsafe request_id) emit none.

    **Telemetry must never interrupt the batch, and must never make an image fail.**
    `emit_event` already swallows its own write and serialisation errors; the extra broad
    except here is because this function is called **inside** the serve's `try`, and also
    inside `generate_one_image` via `on_attempt` — anything escaping from here would be
    caught by `serve_single_image_request`'s broad except, turning a perfectly good serve into
    `ok=false`. The cost of the signal not being written is "the bot may give up early";
    trading an image for that is not worth it. The promise computation is inside the `try`
    too, for the same reason.
    """
    try:
        emit_event("single_image_serving", request_id=request_id,
                   in_band=bool(in_band), phase=phase,
                   beat_within_sec=_serving_beat_within_sec(batch_cfg))
    except Exception as error:  # pylint: disable=broad-except
        # `!r` is kept on purpose: the `try` holds only `emit_event` and config-file
        # arithmetic, and cannot reach the driver.
        print(f"single_image_serving({phase}) failed; serve continues: "
              f"{error!r}", file=sys.stderr)


def serve_single_image_request(port, req: dict, in_band: bool = False) -> None:
    """Produce an image for "a single arbitrary prompt" and emit `single_image_done`.

    - The main prompt comes from req["prompt"] (required); char1 / char2 / undesired are
      optional.
    - Character boxes are handled along two paths depending on `in_band`:
      * **idle one-shot** (in_band=False; the bot spawns it while idle, and after serving,
        main() returns 0 straight away with no batch after it): delete the "extra" character
        boxes from the tail, then **clear the remaining character boxes to empty strings**
        (keeping no content based on the request's char1 / char2 values). NovelAI forces at
        least 1 character box to remain (the last box has no delete button and cannot be
        deleted), so "delete down to 0" is impossible; the right finish is "delete down to
        the minimum + clear the remaining box", so no character with content exists at
        generation time. No character content is filled in at all; leftover batch character
        traits do not seep into this image.
      * **in-band** (in_band=True; one instant generation slotted in while a batch is
        running): never delete boxes — after serving, generate_loop uses
        _refill_character_fields to refill "the current batch character", and that needs
        those character cards to still exist: `fill_character_prompt` locates the card first
        and then writes, and when the card is missing it returns False (and prints a line),
        so the refill effectively never happens.
        (Correction 2026-09-05: this used to say "needs areas[1]", which was the index-based
        lookup before it was changed.)
        Deleting boxes would make the refill fail silently, and the batch character's
        remaining images would be generated with boxes missing. So in-band keeps the existing
        "clear the empty fields + verify empty strings" behaviour (boxes stay, refill still
        works).
    - Before generating, the in-band path runs verify_character_prompt on both char1 / char2
      (the boxes all stay, and even the "clear" case is verified, with expected="" as the
      clearing confirmation), guaranteeing the previous batch character is not generated
      into this single image because of one silently failed clear. The idle path verifies no
      character, since all boxes are already deleted.
    - Produces only 1 image, saved into output/_oneshot/<request_id>/.
    - **Never** touches the resume checkpoint (webrunner_progress.json) — that is for
      resuming batch characters, and a one-shot must not pollute it.
    - The whole thing is wrapped in try/except: any failure emits `ok=false` with a short
      error, and the caller still deletes the request file, guaranteeing "exactly one event
      per request_id, never triggered twice".
    """
    request_id = str(req.get("request_id") or "")
    prompt = req.get("prompt") or ""
    char1 = req.get("char1") or ""
    char2 = req.get("char2") or ""
    undesired = req.get("undesired") or ""
    try:
        if not prompt.strip():
            emit_event("single_image_done", request_id=request_id, ok=False,
                       error="empty prompt")
            print("serve_single_image_request: empty prompt; aborting")
            return
        if not _is_safe_folder_component(request_id):
            # `request_id` is used below **directly as the output folder name**
            # (`SINGLE_IMAGE_OUTPUT_ROOT / request_id`), and it comes from the request file
            # on disk, which the reading side never validated. Measured: `C:\Windows\Temp\x`
            # **replaces the base entirely** (`output/_oneshot` disappears), `../../..` walks
            # out of the repo, and right after that comes `mkdir(parents=True)` plus writing
            # the downloaded image into it. Today's safety relies on the only writer producing
            # only hexadecimal — a property of **the writing side**, not declared here, with a
            # cross-process JSON file in between. As in `cmd_fav_show`'s docstring: the
            # reading side has to state it itself.
            #
            # Treated as a failure rather than falling back to `unknown/`: a path-shaped id
            # can never be in the bot's correlation map (whose keys are produced by
            # `_generate_request_id()`), `_handle_single_image_done` would simply ignore that
            # event, so nobody would receive the image; and quota is this pipeline's
            # bottleneck. Producing an image nobody is destined to collect is pure loss.
            #
            # Placing it **after** the prompt check is deliberate: when the request file is
            # broken `req` is `{}`, `request_id` and `prompt` are both empty, and letting it
            # keep reporting `empty prompt` leaves the existing diagnostic semantics and tests
            # untouched.
            emit_event("single_image_done", request_id=request_id, ok=False,
                       error="invalid request id")
            print(f"serve_single_image_request: unsafe request_id "
                  f"{request_id!r}; aborting", file=sys.stderr)
            return
        # Read the config right here: the "being served" signal must carry a promise computed
        # from the retry delay **in effect at the time**, and the first one has to be emitted
        # before touching the browser. `load_batch_config` never raises.
        batch_cfg = load_batch_config()
        # "Serving starts": placed after the two validation early-exits (a request that
        # failed validation is not served and should not claim to be), and before the first
        # field write (what the bot needs to know is "someone has started handling this
        # one").
        _emit_serving_beat(request_id, in_band, "start", batch_cfg)
        print(f"serve_single_image_request: request_id={request_id!r} "
              f"in_band={in_band} prompt={prompt[:40]!r} char1={bool(char1)} "
              f"char2={bool(char2)} undesired={bool(undesired)}")
        # Main prompt.
        if not with_retry("oneshot_fill_main_prompt",
                          lambda: fill_main_prompt(port, prompt),
                          max_attempts=3, sleep_range=(2, 4)):
            emit_event("single_image_done", request_id=request_id, ok=False,
                       error="main prompt replacement failed")
            return
        human_pause(0.8, 1.5)
        # Character boxes: handling splits on in_band (see the docstring).
        # in-band: deleting boxes would break the batch refill, so always "fill" (empty value
        # = clear), and the boxes stay.
        # idle one-shot: extra boxes are deleted later, then the remaining box is cleared to an
        # empty string, and no character content is filled in, so the character boxes are not
        # touched here at all (not even the ones with values, since they are about to be
        # deleted + cleared).
        if in_band:
            if not with_retry("oneshot_fill_char1",
                              lambda: fill_character_prompt(port, 1, char1),
                              max_attempts=3, sleep_range=(2, 4)):
                emit_event("single_image_done", request_id=request_id, ok=False,
                           error="character 1 replacement failed")
                return
            human_pause(0.6, 1.0)
            if not with_retry("oneshot_fill_char2",
                              lambda: fill_character_prompt(port, 2, char2),
                              max_attempts=3, sleep_range=(2, 4)):
                emit_event("single_image_done", request_id=request_id, ok=False,
                           error="character 2 replacement failed")
                return
            human_pause(0.6, 1.0)
        # undesired: fill it if there is a value, clear the textarea if empty. A missing field
        # returns False and only WARNs.
        if not with_retry("oneshot_fill_undesired",
                          lambda: fill_main_undesired(port, undesired),
                          max_attempts=3, sleep_range=(2, 4)):
            emit_event("single_image_done", request_id=request_id, ok=False,
                       error="undesired replacement failed")
            return
        human_pause(0.8, 1.5)
        if not in_band:
            # idle one-shot: delete the "extra" character boxes from the tail. NovelAI forces
            # at least 1 character box to remain (the last box has no trash button and cannot
            # be deleted, see remove_all_character_slots / remove_character_slot), so after
            # deleting there is usually still 1 box left, and it may hold leftover content
            # from the previous batch. Deleting boxes alone is not enough; "the remaining
            # character boxes" must then be cleared to empty strings, or the leftover traits
            # seep into this single image (measured symptom: only Character 2 deleted,
            # Character 1 carrying the old value).
            remove_all_character_slots(port)
            human_pause(0.4, 0.8)
            # Clear the remaining character boxes. Does not rely on the "Character N" heading
            # still being there (with only 1 box left NovelAI may not show the heading, and
            # count_characters reads 0 while the textarea is still present); goes by
            # find_prompt_areas instead: index 0 is the main prompt, 1.. are the character
            # boxes. Each box is cleared to "" (best-effort, does not raise).
            clear_ok = True
            for area in find_prompt_areas(port)[1:]:
                try:
                    clear_ok = fill_textarea_like(port, area, "") and clear_ok
                    human_pause(0.2, 0.4)
                except Exception as clear_err:  # pylint: disable=broad-except
                    clear_ok = False
                    # `_short_error`: this line sits in the "clear each character box" loop,
                    # so one request may print several of them.
                    print("  WARN: idle one-shot clear character area failed: "
                          f"{_short_error(clear_err)}")
            # Content-oriented verify: 0 character boxes, or every remaining character box is
            # empty after strip. (count==0 is the wrong success criterion — the last box can
            # never be deleted in the first place.)
            residual = [a for a in find_prompt_areas(port)[1:]
                        if _read_textarea_value(port, a).strip()]
            if residual or not clear_ok:
                print(f"  WARN: idle one-shot still has {len(residual)} non-empty "
                      f"character area(s) after trim+clear")
                snap(port, f"oneshot_char_not_cleared_{request_id[:24]}")
                emit_event("single_image_done", request_id=request_id, ok=False,
                           error="character area clear failed")
                return
        # Verify the character values before generating. in-band: verify both (including the
        # clear case; the boxes are all there).
        # idle one-shot: the remaining box was already cleared above + verified by content,
        # so no per-character verification here.
        if in_band:
            if not with_retry(
                    "oneshot_verify_char1",
                    lambda: verify_character_prompt(port, 1, char1),
                    max_attempts=3, sleep_range=(2, 4)):
                emit_event("single_image_done", request_id=request_id, ok=False,
                           error="character 1 verification failed")
                return
            human_pause(0.3, 0.6)
            if not with_retry(
                    "oneshot_verify_char2",
                    lambda: verify_character_prompt(port, 2, char2),
                    max_attempts=3, sleep_range=(2, 4)):
                emit_event("single_image_done", request_id=request_id, ok=False,
                           error="character 2 verification failed")
                return
        snap(port, f"oneshot_ready_{request_id[:24]}")
        # Generate 1 image. `batch_cfg` was read above, before the "serving starts" signal.
        gen_max = batch_cfg["generate_max_retries"]
        gen_delay = batch_cfg["generate_retry_delay_sec"]
        dl_max = batch_cfg["download_max_retries"]
        # `or "unknown"` is dead by construction — the `_is_safe_folder_component` above
        # returns False for the empty string, so this point is never reached with one.
        # Removing it is not just dead-code cleanup: in `ROOT / (x or "y")` the right operand
        # is an `ast.BoolOp`, while the join guard's site criterion is `ast.Name`, so that
        # form **produces not even one row** — no exemption needed, and nothing anywhere
        # records that it was never looked at. Only as a bare name does this site make it
        # onto the books.
        out_dir = SINGLE_IMAGE_OUTPUT_ROOT / request_id
        out_dir.mkdir(parents=True, exist_ok=True)
        previous_src = get_main_image_src(port)
        # Emit one "being served" at the start of every Generate attempt: a serve can run the
        # full `generate_max_retries` attempts (about 15 minutes by default), which the single
        # one at the start cannot cover.
        new_src = generate_one_image(
            port, previous_src, max_retries=gen_max, retry_delay=gen_delay,
            on_attempt=lambda _attempt: _emit_serving_beat(
                request_id, in_band, "generate", batch_cfg))
        if not new_src:
            snap(port, f"oneshot_generate_fail_{request_id[:24]}")
            emit_event("single_image_done", request_id=request_id, ok=False,
                       error="generation failed (no new image)")
            print("serve_single_image_request: generation failed")
            return
        ts = time.strftime("%Y%m%d_%H%M%S")
        save_path = out_dir / f"oneshot_{ts}.png"
        _emit_serving_beat(request_id, in_band, "download", batch_cfg)
        if not download_image_with_retry(port, new_src, save_path,
                                         max_retries=dl_max):
            snap(port, f"oneshot_download_fail_{request_id[:24]}")
            emit_event("single_image_done", request_id=request_id, ok=False,
                       error="download failed")
            print("serve_single_image_request: download failed")
            return
        rel = _single_image_relative_path(save_path)
        emit_event("single_image_done", request_id=request_id, ok=True,
                   path=rel)
        print(f"serve_single_image_request: done -> {rel}")
    except GenerationBlockedError as error:
        # The batch can close the dialog and wait patiently for the quota to refill; a single
        # image cannot: the user is waiting for a reply. But the dialog **must be closed** —
        # left open, it would block the batch that runs next.
        print(f"serve_single_image_request: {error}", file=sys.stderr)
        dismiss_blocking_dialog(port)
        emit_event("single_image_done", request_id=request_id, ok=False,
                   error="quota unavailable")
        return
    except BrowserGoneError as error:
        # The browser is gone entirely: first close out this request cleanly (the caller
        # always deletes the request file, and every request_id must map to exactly one
        # single_image_done event), then propagate it, so run_batch's outer handler wraps up
        # and respawns — swallowing it would leave every later request / every batch image
        # spinning against a dead browser.
        emit_event("single_image_done", request_id=request_id, ok=False,
                   error="browser session lost")
        print(f"serve_single_image_request: {error}", file=sys.stderr)
        raise
    except Exception as error:  # pylint: disable=broad-except
        emit_event("single_image_done", request_id=request_id, ok=False,
                   error=str(error)[:200])
        # `_long_error`: a request only ever reaches this once, and it is that request's
        # terminal report.
        print(f"serve_single_image_request failed: {_long_error(error)}",
              file=sys.stderr)


def rest_until(port, wake_ts: float, *, slice_sec: float = 30.0) -> bool:
    """**Sliced** sleep for the scheduled rest (the `rest_hours` after `schedule_limit_hours`
    is reached).

    This used to be a single `time.sleep(rest_hours * 3600)` — 6 hours by default without
    waking at all. Measured 2026-08-28: after the 06:50:54 `character_done` there was not a
    single event for six whole hours, which from outside looked exactly like "hung". A single
    `time.sleep` blocks three things at once:

    1. `wait_if_paused` — the pause marker could take up to six hours to take effect;
    2. `check_dom_request` — DOM requests likewise wait six hours;
    3. `check_single_image_request` — worse still: the bot's
       `_SINGLE_IMAGE_PENDING_TTL_SEC` is 600 seconds, so single-image requests sent during
       the rest **always** time out, and not one can ever succeed.

    The quota-wait path (`wait_for_quota_recovery`) has long been a sliced sleep + one serve
    per slice, for the reason written in `_serve_pending_requests`'s docstring: sleeping longer
    than the TTL requires interjecting. The rest is six times longer than the quota wait yet
    did not do it — the same approach is copied here.

    Returns "whether any in-band single-image request was served during this rest". If so,
    the caller must reset `prev_*` to None, forcing the next pair to refill every field; a
    single-image serve overwrites the main prompt / characters / undesired, and the per-pair
    diff would think the values were unchanged and skip them (same as the call site at line
    4034). `_refill_character_fields` is **not needed** here — the rest point is between two
    characters, the previous character has already wrapped up, and there are no "current
    character's fields" to rescue.

    A failed interjection is only logged to stderr and not propagated: a Chrome idling for
    hours occasionally acting up is normal, knocking out the whole batch round over one
    hiccup is not worth it, and right after the rest ends comes the
    `restart_chrome_every_n_characters` restart (default 1 = restart for every character), so
    the browser gets replaced with a clean one anyway.
    """
    served = False
    # Wall-clock target → a **monotonic** deadline, converted just this once. The rest is six
    # hours by default, and NTP syncs, manual clock changes and time zone / daylight saving
    # adjustments during it would skew "how much is left" wholesale: the clock set back an
    # hour means an extra hour of sleep, set forward an hour means an hour less, while
    # `rest_hours` means a **duration**, not "sleep until a certain time" (the caller computes
    # exactly `time.time() + rest_s`). `wake_ts` itself stays a wall-clock value — the caller
    # uses it to print "wakes at what time" and also writes it into the `schedule_rest` event
    # for `/rate` and `/eta` — so the conversion lives here, and the caller need not change.
    deadline = time.monotonic() + max(0.0, wake_ts - time.time())
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        wait_if_paused("schedule rest")
        try:
            check_dom_request(port)
            if check_single_image_request(port):
                served = True
        except Exception as error:  # pylint: disable=broad-except
            # `_short_error`: the rest is cut into 30-second slices, 6 hours by default = up
            # to 720 slices; if the browser dies during the rest, this line prints 720 times.
            print(f"  [rest] in-band request failed: {_short_error(error)}",
                  file=sys.stderr)
        # Serving may take a good while, so recompute what is left before sleeping, to avoid
        # oversleeping.
        time.sleep(max(0.0, min(slice_sec, deadline - time.monotonic())))
    return served


def check_single_image_request(port, in_band: bool = True) -> bool:
    """Iteration-boundary poll: when SINGLE_IMAGE_REQUEST_FILE is present, serve 1 image and
    delete the file.

    Returning True means "this turn really served an in-band single-image request"; on
    receiving True the caller (the main loop) must reset prev_prompt / prev_e2 /
    prev_undesired to None, so the batch's next pair refills all its own fields (otherwise the
    per-pair diff would think the values were unchanged, skip them, and use the prompt left
    over from the one-shot). The request file is deleted once handled, success or failure, so
    it does not trigger again. **The resume checkpoint is not touched**.

    `in_band`: whether this serve jumps the queue inside a running batch (default True,
    because both call sites inside the batch are in-band). Only the idle one-shot path at
    main() startup passes False — that one returns 0 straight after serving with no batch
    after it, so it can safely delete every character box entirely (see
    serve_single_image_request / remove_all_character_slots)."""
    if not SINGLE_IMAGE_REQUEST_FILE.exists():
        return False
    req: dict = {}
    try:
        text = SINGLE_IMAGE_REQUEST_FILE.read_text(encoding="utf-8")
        parsed = json.loads(text or "null")
        if isinstance(parsed, dict):
            req = parsed
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        # `UnicodeDecodeError` and `json.JSONDecodeError` are both subclasses of `ValueError`
        # with no inheritance between them, so both must be listed. This **deliberately**
        # swallows (the opposite of the queue readers): req stays empty → serve gets no
        # prompt → emits an ok=false `single_image_done` → finally deletes the request file. A
        # broken single-image request should only fail that request, not take the whole batch
        # of characters currently running down with it.
        print(f"check_single_image_request: bad request file: {error!r}",
              file=sys.stderr)
    try:
        serve_single_image_request(port, req, in_band=in_band)
    finally:
        try:
            SINGLE_IMAGE_REQUEST_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    return True


def _refill_character_fields(port, prompt: str, char1: str, char2: str,
                             undesired: str) -> bool:
    """An in-band single-image serve overwrites the main prompt / character / undesired
    fields; when a batch character is interrupted halfway, after serving "the current
    character's" fields must be refilled, or the rest of that character's images would use
    the one-shot's prompt. Every field (empty strings included) must be refilled and
    verified; if any step fails it returns False so the caller aborts, rather than carrying on
    generating with leftover values.

    ⚠️ **All four fills must be finished before the two verifies. Do not pair and interleave
    them.**

    This is not a style preference; it was measured: **filling Character 2 clears Character
    1.** Counted from `WEBRunner.log` on 2026-09-08 (the 90 refills after honest logging went
    live on 09-03):

    * Character 1 read back a mismatch at verify time **90/90**, and **all** of them were
      `0 vs N` (the field was empty), never `N+k vs N` (content inserted);
    * Character 2 read back a mismatch **0/90** — so the interference is **one-way**;
    * Compared one by one: char1 read the correct content **before writing** and became empty
      only after char2 was filled, **90/90**. In other words the clearing happens at the char2
      step, not because of a page reload.

    The only reason this path can repair itself now is that the verifies come **after both
    fills are done**, so they see the final state. Tidying it into "fill one, verify one" —

        fill_char1 → verify_char1 → fill_char2 → verify_char2

    — is the tidy-up anyone seeing this would want to do (clearer pairing, better locality),
    but then `verify_char1` would pass **before** char2 clears char1, so **char1 is blank for
    the whole batch, without a single log line**: verify green, no `still mismatched`, and
    `with_retry` does not fail either. The consequence is exactly the one this file warns
    about everywhere — quietly producing a whole batch of images with the wrong prompt, and
    this time without even a drift warning. `test_webrunner_shared.py` has a behaviour test
    pinning it down (walking the whole path with a fake port where "filling char2 clears
    char1", and asserting **both fields are correct at the end**).

    **Deliberately does not verify char1 again after `verify_char2`.** The symmetric risk
    (refilling char1 in turn clearing char2) currently has 0/90 counter-evidence; running an
    extra round for it would pay a cost for a direction never observed, and would make the
    step list look even less sensible. Add it when a char2 drift record really shows up — only
    then will it be known how many rounds to add.
    """
    try:
        steps = (
            ("refill_main_prompt", lambda: fill_main_prompt(port, prompt)),
            ("refill_char1", lambda: fill_character_prompt(port, 1, char1)),
            ("refill_char2", lambda: fill_character_prompt(port, 2, char2)),
            ("refill_undesired", lambda: fill_main_undesired(port, undesired)),
            ("refill_verify_char1",
             lambda: verify_character_prompt(port, 1, char1)),
            ("refill_verify_char2",
             lambda: verify_character_prompt(port, 2, char2)),
        )
        for label, action in steps:
            if not with_retry(label, action, max_attempts=3,
                              sleep_range=(2, 4)):
                print(f"  WARN: {label} failed; aborting batch continuation")
                return False
            human_pause(0.4, 0.8)
        return True
    except BrowserGoneError:
        # Returning False would also make the caller raise, but the error message would
        # become "field restore failed", burying the real cause of death (the browser is
        # gone). Propagate it directly to keep the diagnosis.
        raise
    except Exception as error:  # pylint: disable=broad-except
        # `_long_error`: reached only once per interjected serve / per quota reload, so it is
        # not a repeated line.
        print(f"  WARN: re-fill after in-band one-shot failed: "
              f"{_long_error(error)}", file=sys.stderr)
        return False


# ---------- per-character generation loop (P6 C5 — port-based) ---------------
# Generates `images_per_character` images for one character, with crash
# fast-path, consecutive-fail alert/abort, in-band single-image serve, and
# resume-checkpoint `update_saved`. `minimize_fn` is an optional zero-arg
# callback injected by each variant (its win32-augmented minimize, which
# stays per-variant — it reads the deferred Chrome-profile globals); None
# skips minimize (used by the no-browser tests).


def generate_loop(port, character_name: str, batch_cfg: dict,
                  batch_start: float, out_dir: Path | None = None,
                  resume_count: int = 0, refill: tuple | None = None,
                  minimize_fn=None,
                  seen_srcs: collections.deque | None = None,
                  seen_digests: set[str] | None = None) -> int:
    count = batch_cfg["images_per_character"]
    inter_delay = batch_cfg["inter_image_delay_sec"]
    gen_max = batch_cfg["generate_max_retries"]
    gen_delay = batch_cfg["generate_retry_delay_sec"]
    dl_max = batch_cfg["download_max_retries"]
    fail_abort = batch_cfg["consecutive_fail_abort"]
    # `out_dir` is supplied by main() when resuming an interrupted character
    # (so we continue the SAME folder); otherwise allocate fresh.
    if out_dir is None:
        out_dir = allocate_output_dir(character_name, batch_start)
    out_dir.mkdir(parents=True, exist_ok=True)
    if out_dir.name != character_name:
        print(f"  using numbered output folder: {out_dir.name}/")
    # Only used to measure "how long this character ran" (`character_done.elapsed_sec`
    # below), so it uses the **monotonic** clock: a character easily takes hours, and one jump
    # from an NTP sync / time zone change along the way makes the reported duration absurd
    # (a jump back can even make it negative). The `_mono` in the name is deliberate — this
    # value **must not** be used as an absolute timestamp (written into an event's `ts=`,
    # compared with file mtimes, synchronised with another process); monotonic's zero point
    # differs in every process. For an absolute point in time, take `time.time()` separately.
    loop_start_mono = time.monotonic()
    emit_event("character_start", name=character_name, target=count,
               folder=out_dir.name, resumed=resume_count)
    consecutive_fails = 0
    page_recovery_tried = False
    fail_alert_sent = False
    # Tier 2's one-off allowance: when a modal with unrecognised text blocks the page, close
    # it safely first and give it one more chance (the site occasionally pops up
    # announcements / surveys and the like). Allowed only once per character — if it blocks
    # again right after being closed, it really needs a human, and is not something that can
    # be casually closed.
    tier2_dismissed = False
    # Seed `saved` with the images already on disk from a prior interrupted
    # run so the returned total (and the pop gate that compares it to the
    # threshold) reflects the WHOLE folder, not just this run's additions.
    saved = resume_count
    if resume_count > 0:
        _, start_index = _run_progress.folder_image_stats(out_dir)
        print(f"  resuming {character_name} from {resume_count}/{count} "
              f"(next #{start_index:04d}) in {out_dir.name}/")
    else:
        start_index = 1
    remaining = max(0, count - resume_count)
    previous_src = get_main_image_src(port)
    # The srcs already saved. The site occasionally swaps an old image back into the main
    # image area (see `wait_for_new_image`'s docstring for why), and this record is the only
    # thing that can stop "saving the same image again". The cap of 50 is deliberate: all it
    # needs to cover is "the last few the site might swap back", while no cap would let an
    # extremely rare coincidence (the same blob: URL really being reassigned) block that slot
    # forever.
    # These two "things seen" **are shared across characters** — `run_batch` passes the same
    # ones in. The browser document is not rebuilt between characters (only the periodic
    # `port.restart` memory flush does that), so the previous character's images still sit in
    # the site's history area and can be picked just the same. Opening a fresh one per
    # character would empty the defence every time the character changes.
    # If none is passed, open one locally, so standalone calls (tests, the je variant) are
    # still protected.
    if seen_srcs is None:
        seen_srcs = collections.deque(maxlen=50)
    if seen_digests is None:
        seen_digests = set()
    if previous_src:
        seen_srcs.append(previous_src)
    duplicate_alert_sent = False

    def _serve_pending_requests() -> None:
        """Interject the bot's DOM / single-image requests, and refill the batch fields after
        serving.

        Shared by two call sites: between images (as it always was), and **during the hour
        of waiting for quota** (once per sleep slice). The latter is necessary — the bot's
        `_SINGLE_IMAGE_PENDING_TTL_SEC` is 600 seconds, while this sleeps 3600 seconds by
        default.

        A single-image serve overwrites the main prompt / character / undesired fields, so
        after serving, "the current character's" fields must be refilled, or the rest of this
        character's images would use the one-shot's prompt. While quota has not come back,
        the serve itself gets blocked and reports failure — a quick and honest answer for the
        user, better than waiting ten minutes on the chat platform and then being swept away
        as "not served".
        """
        nonlocal previous_src
        check_dom_request(port)
        if check_single_image_request(port) and refill is not None:
            if not _refill_character_fields(port, *refill):
                raise RuntimeError(
                    "failed to restore batch fields after in-band request")
            previous_src = get_main_image_src(port)
            # The interjected image must be recorded too, or it becomes a candidate for the
            # next batch image.
            if previous_src:
                seen_srcs.append(previous_src)

    def _restore_fields_after_reload() -> None:
        """After the quota path has reloaded the page, refill this character's fields.

        `refill` is the `(prompt, char1, char2, undesired)` passed in by `run_batch`; when
        it is not passed (the je variant / standalone test calls) nothing is done — with no
        trustworthy source to fill from, forcing a fill would be worse. If the fields cannot
        be refilled, abort: carrying on with uncertain fields would quietly produce a whole
        batch of images with the wrong prompt, far worse than stopping and letting the
        supervisor respawn + rerun setup.
        """
        if refill is None:
            return
        print("  [quota] page was reloaded; refilling the character fields")
        if not _refill_character_fields(port, *refill):
            raise RuntimeError(
                f"failed to restore the fields for `{character_name}` after "
                f"the quota reload; aborting instead of generating with "
                f"unknown prompt state")

    for n in range(remaining):
        wait_if_paused(f"{character_name} image")
        i = start_index + n
        done_so_far = resume_count + n + 1
        ts = time.strftime("%Y%m%d_%H%M%S")
        target = out_dir / f"{character_name}_{i:04d}_{ts}.png"
        print(f"[{character_name}] generating {done_so_far}/{count} -> {target.name}")
        # Running out of quota (the site popping up a purchase / plan dialog) is not a
        # failure, it is "not your turn yet": close the dialog, wait for the quota to refill,
        # retry **the same image**. It does not count towards consecutive_fails and does not
        # end the process, so the supervisor does not step in, and login + setup are not
        # rerun every round.
        # `quota_waited` accumulates across rounds, which is what makes the
        # `quota_wait_max_sec` cap meaningful; 0 (the default) means waiting with no cap.
        quota_waited = 0.0
        # "How long the last round waited" = the **difference** between the return value and
        # the value passed in, written into `quota_resumed`'s `last_wait_sec`. It is correct
        # by definition, so `quota_wait_poll_sec` need not be read again here — one setting
        # read in two places diverges sooner or later.
        #
        # With a fixed interval this value always equals `quota_wait_poll_sec` and looks
        # redundant, but it is the **only** place that keeps the poll interval in effect at
        # the time in the event stream. On 2026-09-07, answering "would a short poll be
        # faster" meant reconstructing the 08-25 setting change by comparing the timestamps of
        # adjacent events — with this field it can simply be read. The collection cost is
        # zero; what it saves is the next round of archaeology.
        quota_last_wait = 0.0
        while True:
            try:
                new_src = generate_one_image(port, previous_src,
                                             max_retries=gen_max,
                                             retry_delay=gen_delay,
                                             seen_srcs=seen_srcs)
                break
            except GenerationBlockedError:
                if quota_waited == 0.0:
                    emit_event("quota_blocked", character=character_name,
                               image_index=i)
                before_wait = quota_waited
                quota_waited = wait_for_quota_recovery(
                    port, batch_cfg,
                    label=f"{character_name} image {i}",
                    waited_sec=quota_waited,
                    on_reload=_restore_fields_after_reload,
                    on_idle=_serve_pending_requests)
                quota_last_wait = quota_waited - before_wait
        # "Recovered" must be judged by the **result**. `generate_one_image` returns None,
        # not raise, when its retries are used up, so this still breaks out — it used to
        # print "recovered" and emit `quota_resumed` unconditionally, so the user received
        # "quota has recovered" on the chat platform just as the background was about to give
        # up on ten images in a row. Of the 62 "recovered" in the production log 1 was false,
        # and that one was exactly the start of the two-hour idle spin that followed.
        if quota_waited > 0.0 and new_src:
            # `last_wait_sec` is recorded as well: the cumulative value alone does not show
            # what the poll interval was at the time, and that is the one thing needed later
            # to tell "which setting produced this stretch of records".
            print(f"  [quota] recovered after {quota_waited / 60:.0f} min "
                  f"(poll {quota_last_wait / 60:.0f} min); "
                  f"resuming `{character_name}`")
            emit_event("quota_resumed", character=character_name,
                       image_index=i, waited_sec=round(quota_waited, 1),
                       last_wait_sec=round(quota_last_wait, 1))
        elif quota_waited > 0.0:
            print(f"  [quota] waited {quota_waited / 60:.0f} min but the retry "
                  f"still produced nothing for `{character_name}` image {i}",
                  file=sys.stderr)
        if not new_src:
            print(f"  giving up on image {i} after {gen_max} retries")
            snap(port, f"generate_fail_{i:04d}")
            # Fast-path: a renderer crash ("Aw, Snap!" interstitial)
            # makes every hot-path reader return None silently. Catch it
            # on the FIRST failed image instead of waiting for
            # `consecutive_fail_abort` to tick to 10 (~5 min wasted).
            # Refresh is best-effort — supervisor respawns Chrome
            # anyway, but refresh leaves the about-to-die session in
            # cleaner shape.
            if _is_chrome_crash_page(port):
                print(f"  [chrome crash] 'Aw, Snap!' interstitial "
                      f"detected on image {i}")
                snap(port, f"chrome_crash_{i:04d}")
                _try_chrome_refresh(port)
                raise RuntimeError(
                    f"Chrome renderer crashed (interstitial detected) on "
                    f"image {i}/{count} for `{character_name}`; aborting "
                    f"so supervisor respawns Chrome."
                )
            # Fast-path #2: giving up when **not a single image has appeared in this whole
            # session** means the problem is not "this generation did not happen", but that
            # the page is not in a state where it can generate at all. Measured 2026-08-24
            # (`WEBRunner.log` lines 760-930): after the site closed the session, every one of
            # 40 attempts was `timed out waiting for new image (last src=None)`, giving up on
            # ten images in a row and burning **2 hours 28 minutes** before
            # `consecutive_fail_abort` wrapped it up. What actually fixed it was the
            # supervisor's respawn line `no session - going through /login flow`.
            #
            # The criterion uses `previous_src`, which is already at hand — it is "the image
            # on screen before Generate was pressed". A blob value = this session really has
            # produced images and the app is alive, so this failure is sporadic and not worth
            # extra cost; None = not a single image seen. **Do not** switch to `saved`: a
            # resumed character starts with `saved` greater than 0 while the page is brand
            # new, which would reverse the decision entirely.
            #
            # The action is reload + refill, both existing implementations that the quota
            # path exercises once an hour. The session is still there → this is just a cheap
            # recovery (it really does rescue a stuck page); the session is gone → the refill
            # is bound to fail, `_restore_fields_after_reload` raises, and the supervisor
            # respawns and logs in again. 148 minutes thereby shrink to the time of one image.
            #
            # Done only once per character. Afterwards `previous_src` is still None, and
            # without a flag every subsequent failed image would reload once more.
            # When `refill is None` (the je variant / standalone tests) the whole block is
            # skipped: with no trustworthy source to fill from, a reload would only clear the
            # fields, worse than leaving them alone.
            if (previous_src is None and not page_recovery_tried
                    and refill is not None):
                page_recovery_tried = True
                print(f"  [recover] no image has appeared at all in this "
                      f"session; reloading the page before spending more "
                      f"attempts on `{character_name}`", file=sys.stderr)
                emit_event("page_recovery", character=character_name,
                           image_index=i)
                _try_chrome_refresh(port)
                _restore_fields_after_reload()
            consecutive_fails += 1
            if (not fail_alert_sent
                    and consecutive_fails >= CONSECUTIVE_FAIL_ALERT):
                emit_event("consecutive_failures",
                           character=character_name,
                           count=consecutive_fails,
                           image_index=i)
                fail_alert_sent = True
            # Hard abort once we've burned through `fail_abort` images in a
            # row. Without this, a dead Chrome / lost chromedriver session
            # lets the loop silently grind through all 240 images with 0
            # saves and exit rc=0 — `start_webrunner.py` / bot supervisor
            # see "clean finish" and never respawn. Raising propagates to
            # `main()`'s outer except, emits `critical_error`, exits non-
            # zero, supervisor respawns Chrome from scratch.
            if consecutive_fails >= fail_abort:
                # Tier 2 (text not looked at): consecutive failures reached the threshold and
                # a visible modal is still blocking the screen → it needs a human, it is not a
                # crash. A respawn would only see the same dialog.
                blocking = has_blocking_dialog(port)
                if blocking and not tier2_dismissed:
                    tier2_dismissed = True
                    print(f"  [blocked] unrecognised modal dialog is blocking "
                          f"the page: {blocking!r}", file=sys.stderr)
                    if dismiss_blocking_dialog(port):
                        print("  [blocked] dismissed it; giving the character "
                              "one more chance before stopping")
                        consecutive_fails = 0
                        fail_alert_sent = False
                        continue
                if blocking:
                    print(f"  [blocked] {consecutive_fails} consecutive "
                          f"failures with a modal dialog still on screen",
                          file=sys.stderr)
                    print(f"  [blocked] dialog text: {blocking!r}",
                          file=sys.stderr)
                    raise GenerationBlockedError(
                        f"{consecutive_fails} consecutive failures on "
                        f"`{character_name}` with a modal dialog blocking the "
                        f"page; stopping instead of respawning — needs a human."
                    )
                # **Do not** claim "most likely a Chrome crash" here again. By the time this
                # line is reached, those causes have each been ruled out: the 'Aw, Snap!'
                # crash page was checked on the very first failed image
                # (`_is_chrome_crash_page`), a lost session is wrapped up early by
                # `_abort_if_browser_gone`, and a modal is exactly the condition of the if
                # above. Writing already-excluded causes as the "likely cause" has actually
                # misled people: the 2026-08-24 run that gave up on ten images in a row and
                # spun idle for two hours was a page-state problem (the quota reload wiped the
                # prompt just filled in), yet the log kept pointing at Chrome.
                raise RuntimeError(
                    f"{consecutive_fails} consecutive image failures on "
                    f"`{character_name}` (image {i}/{count}); aborting so the "
                    f"supervisor can respawn Chrome with a fresh session. "
                    f"Already ruled out: the 'Aw, Snap!' crash page, a lost "
                    f"driver session, and a modal dialog on screen — so the "
                    f"page is most likely in a state where Generate silently "
                    f"does nothing (prompt fields lost to a reload is the one "
                    f"we have actually seen)."
                )
            continue
        previous_src = new_src
        seen_srcs.append(new_src)
        if download_image_with_retry(port, new_src, target, max_retries=dl_max):
            saved += 1
            consecutive_fails = 0
            fail_alert_sent = False
            # Keep the on-disk checkpoint's `saved` in lock-step with the
            # folder so a sudden kill resumes from the true count (cheap —
            # one atomic write per inter-image delay). Only on success.
            _run_progress.update_saved(saved)
            # The content-level line of defence. Two byte-identical images within the same
            # character mean the site served an old result — before 2026-08-27 this had
            # **no symptom at all**, and was only discovered by comparing the hashes of the
            # output folder afterwards. The log line is written every time; the event is
            # emitted once per character (a bad stretch may run to dozens of images in a row,
            # so do not flood the channel).
            digest = _file_digest(target)
            if digest and digest in seen_digests:
                print(f"  [warn] image {i} is byte-identical to one already "
                      f"saved for `{character_name}`; the site served an old "
                      f"result", file=sys.stderr)
                if not duplicate_alert_sent:
                    duplicate_alert_sent = True
                    emit_event("duplicate_image", character=character_name,
                               image_index=i)
                # Detecting it is not enough — the duplicate would otherwise **stay in the
                # folder and still count as an image**, so "120 images" would really hold
                # only 119 distinct contents, and neither the count nor the files would say
                # so. On 2026-08-27 one pair was actually caught in production output: the
                # same character's #0043 (06:50:19) and #0057 (06:57:27), an identical
                # 1,451,588 bytes, 14 images and 7 minutes apart, with no reaction at all at
                # the src level (the site minted a new blob URL for the same old image).
                #
                # Deleting is an operation that **preserves information**, not destroys it:
                # what is deleted is the second copy, byte-for-byte identical to the first one
                # that stays, and the evidence remains (the log + the event + the image that
                # is kept).
                #
                # Why `saved` must be rolled back along with it: **the folder's file count is
                # the authority for resuming** (`_run_progress.folder_image_stats`). Rolling
                # back only the count without deleting the file leaves 120 images on disk =
                # "this character is done", so a resume would not make it up; deleting the
                # file without rolling back the count leaves the checkpoint's `saved` higher
                # than reality. Only changing both together keeps them in agreement.
                try:
                    target.unlink()
                except OSError as error:  # pragma: no cover
                    # `!r` is kept on purpose: only `OSError` can arrive here, and `str()`
                    # would print that image's full output path.
                    print(f"  [warn] could not remove the duplicate image: "
                          f"{error!r}", file=sys.stderr)
                else:
                    saved -= 1
                    _run_progress.update_saved(saved)
            elif digest:
                seen_digests.add(digest)
        else:
            snap(port, f"download_failed_{i:04d}")
            # The download goes through an in-page fetch; with the window gone it also just
            # returns False. Probe once, symmetrically with the generation path, rather than
            # letting the fail counter slowly climb to the abort.
            _abort_if_browser_gone(port, f"downloading image {i}")
            consecutive_fails += 1
            if (not fail_alert_sent
                    and consecutive_fails >= CONSECUTIVE_FAIL_ALERT):
                emit_event("consecutive_failures",
                           character=character_name,
                           count=consecutive_fails,
                           image_index=i,
                           phase="download")
                fail_alert_sent = True
            if consecutive_fails >= fail_abort:
                raise RuntimeError(
                    f"{consecutive_fails} consecutive image download "
                    f"failures on `{character_name}` (image {i}/{count}); "
                    "aborting so supervisor can respawn the browser."
                )
        if done_so_far < count:
            delay = random.uniform(*inter_delay)
            print(f"  sleep {delay:.1f}s before next image")
            time.sleep(delay)
            # Poll DOM / single-image requests between images — generate_loop is the
            # long-running hot path, so checking here responds much faster than at the top of
            # the main loop. The same closure also hangs on the quota wait's sleep slices;
            # see `_serve_pending_requests`.
            _serve_pending_requests()
            # Move browser windows that ended up on screen back off screen
            # (`hide_browser_windows`). Windows are already off screen when opened, so this is
            # only insurance: a window already outside is left alone, not minimised, and does
            # not steal focus, so repeated calls are cheap. The parameter keeps the name
            # `minimize_fn` only to avoid touching dozens of callers; **do not** add
            # minimising back — minimising / restoring is exactly the action that makes a
            # freshly produced image flash in the foreground (2026-09-22).
            if minimize_fn is not None:
                minimize_fn()
    # Final line of defence: not a single image of the whole character was saved, and a modal
    # is still blocking the screen.
    # The Tier 2 check above hangs on the `consecutive_fail_abort` threshold, so characters
    # whose `images_per_character` is below the threshold (or that finished early) never
    # reach it — that case would quietly return saved=0, leaving run_batch's zero-output
    # backstop to catch it slowly, with the diagnosis degraded to "nothing was produced"
    # instead of "something is blocking".
    if saved == 0 and remaining > 0:
        blocking = has_blocking_dialog(port)
        if blocking:
            print(f"  [blocked] `{character_name}` saved nothing and a modal "
                  f"dialog is still on screen: {blocking!r}", file=sys.stderr)
            raise GenerationBlockedError(
                f"`{character_name}` produced no images and a modal dialog is "
                f"blocking the page; stopping instead of respawning — needs a "
                f"human.")
    emit_event("character_done",
               name=character_name,
               saved=saved,
               target=count,
               folder=out_dir.name,
               # What is sent is a **duration**, not a point in time: the bot's
               # `_seconds_per_image` uses it to compute `elapsed_sec / saved`, and
               # `_handle_event` formats it as a duration; neither compares it with `now`, so
               # switching to monotonic keeps the meaning, and is more accurate.
               elapsed_sec=round(time.monotonic() - loop_start_mono, 1))
    return saved


# ---------- batch orchestration (P6 C6 — port-based) ------------------------
# `run_batch` is the whole webrunner session body: initial setup (per-variant
# `setup_fn` callback — it uses WebDriverWait/login which stay per-variant),
# the startup single-image server, and the dynamic per-character batch loop
# (re-read queues each char via `read_queues`, decide via `_queue_consume`,
# padding-aware pop, `end` sentinel, resume checkpoint). Chrome restarts go
# through `port.restart`; `minimize_fn` threads each variant's win32 minimize
# into generate_loop. The variant `main()` is a thin shell: preflight (via
# `run_preflight`, before Chrome boot) -> boot -> run_batch -> finally quit+sync.


def read_queues():
    real_p = read_todo_characters(TODO_PROMPT_FILE)
    eff_p, fb_p = list(real_p), False
    if not real_p:
        fb_text = read_text_safe(PROMPT_FILE)
        if fb_text:
            eff_p, fb_p = [fb_text], True
    real_1 = read_todo_characters(TODO_FILE_1)
    eff_1, fb_1 = list(real_1), False
    if not real_1:
        fb_text = read_text_safe(CHARACTER1_FALLBACK_FILE)
        if fb_text:
            eff_1, fb_1 = [fb_text], True
    # Character 2 is positional. A blank row explicitly means remove its UI
    # card for this pair; it must not be filtered out or replaced by fallback.
    real_2 = read_todo_characters(TODO_FILE_2, preserve_blank=True)
    eff_2, fb_2 = list(real_2), False
    if not real_2:
        fb_text = read_text_safe(CHARACTER2_FALLBACK_FILE)
        if fb_text:
            eff_2, fb_2 = [fb_text], True
    # An empty todo_undesired falls back to the whole content of undesired.md; if
    # undesired.md is empty too, all entries are empty strings (fill_main_undesired still
    # clears the current textarea).
    real_u = read_todo_characters(TODO_UNDESIRED_FILE)
    eff_u, fb_u = list(real_u), False
    if not real_u:
        fb_text = read_text_safe(UNDESIRED_FILE)
        if fb_text:
            eff_u, fb_u = [fb_text], True
    return ((real_p, real_1, real_2, real_u),
            (eff_p, eff_1, eff_2, eff_u),
            (fb_p, fb_1, fb_2, fb_u))


def parse_run_mode(argv) -> str:
    """Recognise this round's mode from argv (for why it has to be declared, see the incident
    record above `RUN_MODE_BATCH`). Returns `RUN_MODE_SINGLE_IMAGE_SERVER` or
    `RUN_MODE_BATCH`.

    All three decision rules are deliberate:

    * **Only `argv[1:]` is looked at; `argv[0]` never counts as a hit.** `argv[0]` is the
      path of the script being run, not an option, and scanning it too would add a switch
      that flips the mode whenever "the repo happens to live under a path whose name equals
      this flag" — somewhere nobody would ever think to check. It also lets callers and tests
      safely feed the standard shape `["prog", ...]`.
    * **Only whole-element equality counts; no prefix / substring matching.** So
      `--single-image-server=1` and `--no-single-image-server` both do **not** hit.
      Supporting those shapes later would be a deliberate change, not something that happens
      by a slip of the hand.
    * **Repeats do not change the result; unrecognised arguments are always ignored, with no
      error.** This function answers one yes/no question. If it also took on "are there
      unrecognised arguments", any new flag not yet wired up here would make the webrunner
      die right at the entry point: non-zero rc → the supervisor respawns → the same argv →
      dies again, turning a harmless argument drift into endless respawning.
    """
    return (RUN_MODE_SINGLE_IMAGE_SERVER
            if SINGLE_IMAGE_SERVER_FLAG in tuple(argv)[1:]
            else RUN_MODE_BATCH)


def run_preflight(oneshot_pending: bool) -> bool:
    """Read the four queues, print the run preview, and return
    True if there is work (or a pending one-shot). False (caller
    returns rc=1) when nothing to do — checked BEFORE Chrome boot.

    The source of `oneshot_pending` changed in pass 3 (2026-09-12): both variants now feed it
    `mode == RUN_MODE_SINGLE_IMAGE_SERVER` (the **declared** mode), no longer
    `SINGLE_IMAGE_REQUEST_FILE.exists()` (an **inference**). The difference is substantive:
    an old request file nobody cleared used to make a batch round with empty queues open
    Chrome for nothing, and then fall into the startup branch's hijack path (see the incident
    record above `RUN_MODE_BATCH`).

    ⚠️ **Do not rename the parameter.** One of the three callers uses it as a keyword: the
    repo root's `run_batch.py` (`run_preflight(oneshot_pending=False)`; that is the console
    entry point and is itself never a single-image server — it only hands the work over to
    `start_webrunner.py`, which does not forward its own arguments when building argv, so the
    child process never sees the flag either). A rename would make it raise `TypeError` at
    run time, at an entry point that is only reached when someone runs it by hand.
    """
    real0, eff0, fb0 = read_queues()
    preview_pairs = pair_todos(*eff0)
    if not preview_pairs and not any(real0) and not oneshot_pending:
        print("no entries in todo_prompt / todo_character1 / todo_character2; nothing to do")
        return False
    print(
        f"todo quadruples: {len(preview_pairs)} "
        f"(prompt={len(real0[0])}, char1={len(real0[1])}, char2={len(real0[2])}, "
        f"undesired={len(real0[3])}) [dynamic: re-read each character]"
    )
    for i, (ep, e1, e2, eu) in enumerate(preview_pairs, 1):
        print(
            f"  [{i}] prompt={(ep[:40] + '…') if len(ep) > 40 else ep!r}"
            f" char1={character_folder_name(e1) if e1 else '(none)'!r}"
            f" char2={character_folder_name(e2) if e2 else '(none)'!r}"
            f" undesired={(eu[:30] + '…') if len(eu) > 30 else eu!r}"
        )
    return True


def _serve_single_image_queue(port) -> None:
    """The resident single-image serve loop: serve the whole single-image queue, and return
    only after being idle long enough.

    `run_batch` calls this when `mode == RUN_MODE_SINGLE_IMAGE_SERVER` (the mode is declared
    by argv; for the incident record and the rules see the passage above `RUN_MODE_BATCH`).
    After setup it is not "serve 1 image and finish": every request served resets the idle
    clock and polls again immediately, and it only wraps up after about `idle_timeout`
    seconds idle with no new request. Every serve is an idle one-shot (`in_band=False` =
    delete every character box, keeping only the main prompt), so the previous batch's
    characters never seep into a one-shot image.

    ⚠️ **On entry the request file is not guaranteed to exist, and that is the norm.** The
    bot spawns us first and then pumps the request onto disk, so the first turn may well
    pick up nothing — the loop waits anyway, and the request usually lands within a few
    hundred milliseconds. That is exactly why the caller's gate **must not** also `and`
    `SINGLE_IMAGE_REQUEST_FILE.exists()`: written that way, a real single-image server would
    fall into the batch loop and run the whole todo queue.
    """
    # Local constants of the resident single-image serve loop (no new module constant — this
    # is a tuning value specific to the serve loop).
    idle_timeout = 120.0  # wrap up after this long idle with no new request
    print(f"single-image server: serving queue, idle timeout "
          f"{idle_timeout:.0f}s")
    # The idle clock's reference point. It measures "how long since the last serve" = an
    # interval, so it uses the **monotonic** clock: the wall clock jumping back (an NTP sync,
    # the user changing the clock) would keep this server from ever shutting down, and
    # jumping forward would wrap up early while requests are still queued.
    last_served_mono = time.monotonic()
    while True:
        # check_single_image_request returns True only when "the request file exists and
        # was really served".
        served = check_single_image_request(port, in_band=False)
        if served:
            last_served_mono = time.monotonic()
            continue  # poll again immediately: more requests may be queued behind it
        # Nothing to serve this turn.
        if time.monotonic() - last_served_mono >= idle_timeout:
            break
        time.sleep(random.uniform(1.0, 2.0))  # short-interval idle poll
    print(f"single-image server: idle {idle_timeout:.0f}s, shutting down")
    # drain: pick up a request the bot os.replace'd in between our last poll and the break
    # decision above (done once, best-effort, no loop). Missing it would leave that request
    # stuck until the bot's own inflight TTL heals it.
    check_single_image_request(port, in_band=False)


def run_batch(port, email, password, *, setup_fn, minimize_fn,
              mode: str = RUN_MODE_BATCH) -> int:
    """Orchestrate one webrunner session: initial setup (via the
    per-variant `setup_fn`), the startup single-image server, and the
    dynamic per-character batch loop (`_queue_consume`), then the
    end-sentinel / zero-save (rc=3) / done post-loop. Chrome restarts
    go through `port.restart`; `minimize_fn` is threaded into
    generate_loop. Returns the process rc; raises on critical error
    (caller's finally still quits + syncs the profile).

    `mode` declares whether this round is a batch (the default) or a single-image server; it
    comes from `parse_run_mode(sys.argv)`, and the startup branch below **looks only at it**
    (the rules and the incident record are in the passage above `RUN_MODE_BATCH`). **It must
    not also `and` `SINGLE_IMAGE_REQUEST_FILE.exists()`** — the reason is written next to that
    branch, and in one sentence it is: when a real single-image server gets there, the file
    may not have landed yet. The default is `RUN_MODE_BATCH`, so no declaration means batch.

    An unrecognised `mode` **falls back to batch and prints a loud line**, without raising.
    Two reasons: (a) the value can only come from `parse_run_mode`, which can only return
    those two constants, so a third value means the caller wrote it wrong; and the raise
    would come after Chrome has already been opened, when that round has very likely passed
    `rapid_fail_threshold_sec`, so "one mistyped string" would turn into the supervisor
    respawning forever. (b) Falling back to batch is the safe direction: the batch still
    serves pending single-image requests in-band at pair boundaries, so misjudging it as a
    batch at most runs what was queued to run anyway; misjudging it the other way round, as a
    server, is exactly the "whole queue quietly skipped + a fake success rc" the incident
    record above describes.
    """
    if mode not in (RUN_MODE_BATCH, RUN_MODE_SINGLE_IMAGE_SERVER):
        print(f"unknown run mode {mode!r}; falling back to {RUN_MODE_BATCH!r}",
              file=sys.stderr)
        mode = RUN_MODE_BATCH
    # Printed deliberately: that incident was reconstructed from the log, and at the time the
    # log had **not one** line that could say "who this process thinks it is", only what it
    # did.
    print(f"webrunner run mode: {mode}")
    print("webrunner shared revision: prompt-strict-retry-v5 "
          f"({Path(__file__).resolve()})")
    # Ask the operating system not to interrupt this process for the whole batch (**not** to
    # keep the system from entering standby — those are two different things; for the
    # difference and the measurements see the block comment above `StayAwake`). It lives in
    # `run_batch` rather than in each variant's `main()` because this is the layer the two
    # variants share — written here, there can be no "only one side has it" drift. `finally`
    # always runs; even if it does not (the process is hard-killed), the OS reclaims the power
    # request as the process disappears, so no request is left in effect forever.
    _awake = StayAwake()
    if load_batch_config().get("keep_system_awake", True):
        _got = _awake.acquire()
        # All three branches deliberately print `active`'s literal value too: the prose gets
        # rewritten as understanding changes (this passage was rewritten once on 2026-09-20),
        # while the two tokens `power-request` / `execution-state` are what the program
        # really branches on, and only by grepping the log afterwards can one ask "which one
        # did that round actually get".
        if _got == "power-request":
            print("  [power] power request obtained (power-request); the system still enters "
                  "standby, but this process will not be suspended")
        elif _got == "execution-state":
            # If this machine only has S0ix, this path neither blocks standby nor protects the
            # process — saying so plainly beats pretending it succeeded.
            print("  [power] only the old execution-state flag was obtained (execution-state); "
                  "it does not protect this process on a Modern Standby machine")
        else:
            print("  [power] could not obtain either kind of power request; this process may be "
                  "suspended during standby")
    # These two counters **must** be outside the try: the except below reads them to decide
    # "did this round actually produce anything", and the exception may happen before they
    # are assigned (during setup).
    total_saved = 0
    produced = 0
    # Shared by the whole browser session (see the comment in `generate_loop` for why).
    session_srcs: collections.deque[str] = collections.deque(maxlen=50)
    session_digests: set[str] = set()
    try:
        # Full setup (login → model → characters → resolution → sampler →
        # minimize). Extracted into `_setup_session` so the mid-run Chrome
        # restart (memory flush) can re-run the exact same sequence.
        if not setup_fn():
            print("session setup failed; aborting")
            return 2

        # The startup single-image serve branch. **The mode is declared, not inferred from
        # disk** — the inference was wrong once, and the incident record is in the passage
        # above `RUN_MODE_BATCH`. This whole path runs no batch, never increments `produced`
        # (the rc=3 zero-save backstop further down is never reached; the return 0 here comes
        # before it), and never writes the resume checkpoint.
        #
        # ⚠️ **Never `and` `SINGLE_IMAGE_REQUEST_FILE.exists()` here.** It looks safer, but
        # it is actually the mirror image of today's defect: the bot **spawns first and then
        # pumps the request onto disk**, so when a real single-image server gets here, the
        # request file may well not have landed yet; written that way, it would fall into the
        # batch loop below and run the whole todo queue, just as silently.
        if mode == RUN_MODE_SINGLE_IMAGE_SERVER:
            print("run mode declared on argv: single-image server; "
                  "serving the one-shot queue")
            _serve_single_image_queue(port)
            return 0  # wrap up cleanly, so the supervisor does not respawn
        if SINGLE_IMAGE_REQUEST_FILE.exists():
            # On the batch path a request file that is seen is **not deleted**: it is the
            # input for in-band serving, and `check_single_image_request` at the top of the
            # batch loop and between images will pick it up. One line is printed because "a
            # request already lying on disk when the batch starts" is exactly the scene of the
            # 2026-06-27 incident — the log has to show that this round **deliberately** did
            # not treat it as an identity declaration.
            print("batch mode: a single-image request is already on disk; "
                  "it stays there and will be served in-band at the first "
                  "character boundary", file=sys.stderr)

        # Count completed characters so we can cycle Chrome every N of them
        # (memory-flush — see `restart_chrome_every_n_characters`).
        chars_completed = 0
        # Schedule timer: measures "how long this round has been working" (against
        # `schedule_limit_hours`), purely an interval → **monotonic** clock. The wall clock
        # jumping back would postpone the rest indefinitely (that rest period is deliberate),
        # and jumping forward would rest early for no reason.
        #
        # The control group is a few lines below: `iter_batch_start` must stay on
        # `time.time()`, because `allocate_output_dir` compares it against **file mtimes**
        # (`max(mtimes) >= batch_start`), and mtimes are wall-clock epoch. The criterion is
        # "does this value leave this process": landing on disk / in events / compared with
        # another process → `time.time()`; only measuring elapsed time within the process →
        # `time.monotonic()`. The two are not interchangeable; monotonic's zero point differs
        # in every process.
        schedule_start_mono = time.monotonic()
        # `batch_start` is per-iteration: each pair takes `time.time()` only right before
        # entering generate_loop. This makes a pair with "the same char_name but a changed
        # prompt / undesired" automatically fall through to `<name>_2` / `_3` … — the files
        # the previous iteration wrote have mtime < this iteration's batch_start,
        # `_folder_belongs_to_batch` returns False, and it gets numbered.
        prev_prompt: str | None = None
        prev_e2: str | None = None
        prev_undesired: str | None = None
        # The two in-run counters of dynamic consumption (see _queue_consume):
        # - produced: the number of characters actually produced this round, for the chrome
        #   restart cadence / log.
        # - skip: the number of entries this round that "did not reach the threshold, stay at
        #   the front of the queue, and have been attempted" (the cursor).
        #   Each turn takes pairs[skip], and a pop pops at the cursor position (skip==0 is the
        #   old front-pop).
        skip = 0
        # Under the dynamic model `len(pairs)` differs every turn; the post-loop event needs a
        # notion of "the number of pairs this round went through", represented by the current
        # pairs count computed on the last turn.
        last_pairs_len = len(pair_todos(*read_queues()[1]))
        # Set True when an `end` sentinel in the main prompt queue stops the
        # run early; lets the post-loop code skip the zero-save backstop and
        # exit rc=0 (clean finish, no supervisor respawn).
        end_sentinel_hit = False
        while True:
            wait_if_paused("pair boundary")
            # Every turn re-reads the four real queues and applies the fallbacks → eff + fb
            # flags.
            real, eff, fb = read_queues()
            (real_p, real_1, real_2, real_u) = real
            (eff_p, eff_1, eff_2, eff_u) = eff
            (fb_p, fb_1, fb_2, fb_u) = fb
            decision = _queue_consume.decide(
                real_p, real_1, real_2, real_u,
                eff_p, eff_1, eff_2, eff_u,
                fb_p, fb_1, fb_2, fb_u, skip, produced)
            if decision.action == _queue_consume.ACTION_BREAK:
                break
            pairs = decision.pairs
            last_pairs_len = len(pairs)
            # `fallback_single`: the real queues are all empty, but there is a fallback and
            # nothing has been produced this round yet → produce just this one and wrap up
            # (so a length-1 fallback does not stretch a drained real queue into redundant
            # ghost characters). After producing, it breaks at the end of the pop section.
            fallback_single = (
                decision.action == _queue_consume.ACTION_FALLBACK_SINGLE)
            (prompt_entry, entry1, entry2, undesired_entry) = decision.batch
            # `end` sentinel in the MAIN prompt queue terminates the run here:
            # this pair and every later pair are skipped (no generation). Per
            # the queue contract we consume ONLY the `end` line itself (pop it
            # from todo_prompt) and leave any entries after it for the next
            # run. Matched case-insensitively; read_todo_characters already
            # stripped the line. Skipped when the prompt came from the
            # prompt.md fallback (no queue line to pop).
            if not fb_p and _queue_consume.is_end_marker(prompt_entry):
                print(f"  encountered 'end' sentinel at cursor {skip} "
                      f"({len(pairs)} pair(s) this read); stopping run "
                      f"(later pairs skipped)")
                # Remove the first end line from the re-read real_p and write it back (real_p
                # is the disk content at this moment, equivalent to reconciling first).
                for idx, rp in enumerate(real_p):
                    if _queue_consume.is_end_marker(rp):
                        real_p.pop(idx)
                        write_todo_characters(TODO_PROMPT_FILE, real_p)
                        print(f"  consumed 'end' line; "
                              f"{len(real_p)} prompt entr"
                              f"{'y' if len(real_p) == 1 else 'ies'}"
                              f" remain in {TODO_PROMPT_FILE.name}")
                        break
                end_sentinel_hit = True
                break
            char_name = character_folder_name(entry1) if entry1 else character_folder_name(entry2)
            print(f"\n=== {char_name} ===")
            # Code drift check at the character boundary (purely diagnostic, changes no
            # behaviour). It sits under the character banner so the drift line lands right
            # next to it and is easy to match up later; when `drifted is False` it stays
            # completely silent, so normally nothing shows here. One shared copy → takes
            # effect in both variants at once, with no need for each to copy it.
            report_code_drift()
            # Iteration-boundary polling point: after the bot side's `!introspect_dom` writes
            # the request file, it is fulfilled here and emit_event('dom_result') is called.
            check_dom_request(port)
            # In-band single-image request (one instant generation slotted in between batch
            # characters). The serve overwrites the main prompt / character / undesired
            # fields, so after serving prev_* are reset to None, forcing the pair below to
            # refill every one of its own fields (otherwise the per-pair diff would think the
            # values were unchanged, skip them, and use the one-shot's leftover values) — same
            # reason as the reset after a chrome restart.
            if check_single_image_request(port):
                prev_prompt = None
                prev_e2 = None
                prev_undesired = None
            # `human_pause(0.8, 1.5)` goes after every successful fill, imitating the rhythm
            # of "a human finishes one field, pauses, then clicks the next"; React components
            # also use this gap to sync state (so the previous fill's input event is not
            # interfered with by the next fill before it has been processed).
            if prompt_entry != prev_prompt:
                print(f"  filling main prompt ({len(prompt_entry)} chars)")
                if not with_retry(
                        "fill_main_prompt",
                        lambda p=prompt_entry: fill_main_prompt(port, p),
                        max_attempts=3, sleep_range=(2, 4)):
                    print("  skipping - main prompt replacement failed")
                    _abort_if_chrome_crashed(
                        port, f"fill_main_prompt ({char_name})")
                    skip += 1
                    continue
                prev_prompt = prompt_entry
                human_pause(0.8, 1.5)
            # Fill the undesired-content textarea whenever it changed, including
            # the first empty value. Empty is an explicit clear operation; if it
            # cannot be confirmed, skip this pair instead of inheriting stale text.
            if undesired_entry != prev_undesired:
                print(f"  filling undesired ({len(undesired_entry)} chars)")
                ok = with_retry("fill_main_undesired",
                                lambda u=undesired_entry: fill_main_undesired(port, u),
                                max_attempts=3, sleep_range=(2, 4))
                if ok:
                    prev_undesired = undesired_entry
                else:
                    print("  skipping - undesired replacement failed")
                    _abort_if_chrome_crashed(
                        port, f"fill_undesired ({char_name})")
                    skip += 1
                    continue
                human_pause(0.8, 1.5)
            else:
                # Just sync the tracker; no disk write, no DOM change.
                prev_undesired = undesired_entry
            if not with_retry(
                    "set_character2_state",
                    lambda enabled=bool(entry2): set_character2_enabled(
                        port, enabled),
                    max_attempts=3, sleep_range=(2, 4)):
                print("  skipping - Character 2 UI state could not be updated")
                _abort_if_chrome_crashed(
                    port, f"set_character2_state ({char_name})")
                skip += 1
                continue
            if not entry2:
                prev_e2 = None
            if not with_retry(
                    "fill_char1",
                    lambda e=entry1: fill_character_prompt(port, 1, e),
                    max_attempts=3, sleep_range=(2, 4)):
                print("  skipping — char1 fill failed")
                _abort_if_chrome_crashed(port, f"fill_char1 ({char_name})")
                skip += 1
                continue
            human_pause(0.8, 1.5)
            # Only re-fill character 2 when its prompt actually changed.
            if entry2 and entry2 != prev_e2:
                if not with_retry(
                        "fill_char2",
                        lambda e=entry2: fill_character_prompt(port, 2, e),
                        max_attempts=3, sleep_range=(2, 4)):
                    print("  skipping - char2 replacement failed")
                    _abort_if_chrome_crashed(
                        port, f"fill_char2 ({char_name})")
                    skip += 1
                    continue
                prev_e2 = entry2
                human_pause(0.8, 1.5)
            # The verify at fill time cannot catch pollution after the fill (a leftover
            # autocomplete dropdown hit by a later coordinate click, inserting a suggested tag
            # at the end), so each is verified once more before entering generate_loop. entry2
            # is verified even if it was not refilled this round — the field may still have
            # been polluted by the previous round's clicks.
            character_prompts_ok = True
            for char_index, expected in ((1, entry1), (2, entry2)):
                if char_index == 2 and not expected:
                    continue
                if not with_retry(
                        f"verify_char{char_index}",
                        lambda i=char_index, e=expected:
                            verify_character_prompt(port, i, e),
                        max_attempts=3, sleep_range=(2, 4)):
                    print(f"  skipping - Character {char_index} prompt "
                          f"could not be verified")
                    _abort_if_chrome_crashed(
                        port, f"verify_char{char_index} ({char_name})")
                    character_prompts_ok = False
                    break
            if not character_prompts_ok:
                skip += 1
                continue
            snap(port, f"ready_{char_name[:30]}")
            # Per-iteration batch_start: each pair's own "now" is the baseline, so when
            # `allocate_output_dir` sees older files in a same-named folder it numbers the new
            # one `<name>_2` / `_3` …, and a changed prompt gets saved separately.
            iter_batch_start = time.time()
            # Hot-reload batch params per character. Edits to batch_config.json
            # made mid-run take effect on the NEXT character (not mid-batch
            # — count / inter_delay must stay stable for the 240-image loop).
            batch_cfg = load_batch_config()
            target_count = batch_cfg["images_per_character"]
            # Resume an interrupted character. Only the FIRST pending pair of
            # this run could have been mid-flight last time (later pairs never
            # started). In the dynamic model that's `produced == 0 and skip == 0`
            # (the very first batch actually generated this run, before any pop
            # or retained-skip). If the saved checkpoint describes exactly this
            # pair (prompt unchanged), continue its existing output folder
            # instead of regenerating from image 1. Any queue edit → no match.
            resume_dir: Path | None = None
            resume_count = 0
            if produced == 0 and skip == 0:
                prog = _run_progress.read_progress()
                # `folder` has to be validated separately — `matches()` only compares the
                # four identity fields and never looks at it. This used to be a plain
                # `OUTPUT_ROOT / prog["folder"]`, and all six ways of breaking it (field
                # missing, None, int, list, `../`, an absolute path) got through; the
                # details and measured results are in `_run_progress.resume_folder`'s
                # docstring.
                cand = _run_progress.resume_folder(prog, OUTPUT_ROOT)
                # Every branch of this elif chain has to leave a note of "why it did not
                # resume". Before, only the identity-mismatch branch spoke; all the rest were
                # silent fallthroughs — on 2026-08-23 `priestess` opened four numbered folders
                # in a row, burned an hour and a half, and never even reached
                # `character_done`, and afterwards not a single word about it could be found
                # in the log or the events.
                if prog is None:
                    print(f"  resume: no checkpoint on disk; starting "
                          f"`{char_name}` from image 1")
                elif not _run_progress.matches(prog, prompt_entry, entry1,
                                               entry2, undesired_entry):
                    # Checkpoint exists but the first pair changed — DIAGNOSTIC.
                    # Don't resume (a different pair into the same folder would
                    # mix prompts), but log exactly WHICH field diverged so the
                    # real cause (queue edit / fallback flip / whitespace) is
                    # visible in the user's webrunner.log.
                    diffs = _run_progress.diagnose_mismatch(
                        prog, prompt_entry, entry1, entry2, undesired_entry)
                    print(f"  resume: checkpoint for {prog.get('folder')!r} "
                          f"does NOT match first pair `{char_name}`; starting "
                          f"fresh. Diverging fields:")
                    for line in diffs:
                        print(f"    {line}")
                    # The lines above live only on stdout. The console gets closed and the log
                    # file gets trimmed, but "why this round did not resume" is the one thing
                    # anyone wants to know afterwards, so an event is emitted too —
                    # events.ndjson survives across processes, and the bot can read it.
                    # Only the **field names** are sent, not the content: the field content is
                    # the full prompt text, which has no business going into the event file or
                    # being reposted; the human-readable stored/current stays in the log above.
                    emit_event(
                        "resume_mismatch", name=char_name,
                        folder=prog.get("folder"),
                        fields=_run_progress.mismatch_fields(
                            prog, prompt_entry, entry1, entry2,
                            undesired_entry))
                elif cand is None:
                    # The identity fields match but `folder` is unusable = the checkpoint was
                    # corrupted or overwritten by something else. Starting over from image 1
                    # is the only safe choice, but this is not a normal situation, so it goes
                    # to stderr and emits an event.
                    print(f"  resume: checkpoint matches `{char_name}` but its "
                          f"folder value is unusable "
                          f"({prog.get('folder')!r}); starting from image 1",
                          file=sys.stderr)
                    emit_event("resume_unusable", name=char_name,
                               reason="bad_folder")
                else:
                    existing, _ = _run_progress.folder_image_stats(cand)
                    # The file count on disk is the authority, but the checkpoint's saved is
                    # the backup: right after a hard kill the folder listing may briefly
                    # disagree (antivirus locking files and the like), so take the larger of
                    # the two, capped at the target.
                    stored_saved = prog.get("saved", 0)
                    if not isinstance(stored_saved, int) or stored_saved < 0:
                        stored_saved = 0
                    effective = min(max(existing, stored_saved), target_count)
                    if not cand.exists():
                        # The folder the checkpoint points to is gone — most commonly the
                        # user cleared output/ themselves. If saved>0, some work really was
                        # thrown away, which is worth speaking up about.
                        print(f"  resume: the checkpoint folder for "
                              f"`{char_name}` is gone "
                              f"(checkpoint={stored_saved}); starting from "
                              f"image 1", file=sys.stderr)
                        emit_event("resume_unusable", name=char_name,
                                   reason="folder_missing", saved=stored_saved)
                    elif effective <= 0:
                        # The previous round was interrupted before the first image was
                        # saved, so there is nothing to continue. This is normal (not a
                        # fault): log it, emit no event.
                        print(f"  resume: checkpoint for `{char_name}` has no "
                              f"images on disk yet; starting from image 1")
                    elif effective < target_count:
                        resume_dir = cand
                        resume_count = effective
                        print(f"  resume: `{char_name}` {effective}/"
                              f"{target_count} in {cand.name}/ (folder={existing}"
                              f", checkpoint={stored_saved}); continuing")
                    else:
                        # Completed last run but never popped (e.g. crash before
                        # the pop). Reuse the folder; generate_loop adds 0 and
                        # the pop below fires.
                        resume_dir = cand
                        resume_count = effective
                        print(f"  resume: `{char_name}` already complete "
                              f"({effective}/{target_count}) in {cand.name}/; "
                              f"will pop")
            out_dir = resume_dir if resume_dir else allocate_output_dir(
                char_name, iter_batch_start)
            # Checkpoint THIS pair before generating so a mid-character
            # interrupt can resume it next run. Written once; the folder itself
            # tracks how many are done. Cleared on a successful pop below.
            _run_progress.write_progress(prompt_entry, entry1, entry2,
                                         undesired_entry, out_dir.name,
                                         target_count)
            saved = generate_loop(port, char_name, batch_cfg,
                                     iter_batch_start, out_dir=out_dir,
                                     resume_count=resume_count,
                                     refill=(prompt_entry, entry1, entry2,
                                             undesired_entry),
                                     minimize_fn=minimize_fn,
                                     seen_srcs=session_srcs,
                                     seen_digests=session_digests)
            total_saved += saved
            produced += 1
            print(f"  saved {saved}/{target_count} images")

            # Pop consumed entries only when the target was (near) fully saved.
            # Don't touch fallback files — prompt.md / character2.md /
            # undesired.md are persistent defaults, not one-shot queue entries.
            #
            # CRITICAL — padding-aware pop. `pair_todos` pads a SHORTER list by
            # repeating its LAST entry, so e.g. todo_prompt=[P] paired with
            # todo1=[a,b,c] yields prompt column [P,P,P]. That last remaining
            # entry is still needed by every later (padded) pair, so popping it
            # the moment its first occurrence completes drains the short queue
            # ahead of the long one. On a mid-run cancel the file is then left
            # emptied (count mismatch) and a restart falls back to prompt.md for
            # the rest. Guard: only pop the cursor entry when it's NOT a padded
            # tail repeat (cursor index < len-1) OR this is the final batch
            # (nothing left to reuse the tail). `should_pop_at` captures that
            # (skip==0 reduces exactly to the old `_can_pop` front-pop).
            is_last = (len(pairs) - skip) <= 1
            # A returning generate_loop walked the FULL image count (it only
            # raises on a renderer crash / consecutive_fail_abort, both of
            # which bypass this pop via main()'s outer except), so the
            # character is finished. Requiring an exact `saved == target` was
            # too strict: one scattered transient generate/download miss left
            # saved at e.g. 239/240, the entry was retained, and the whole
            # character silently regenerated next run — the probabilistic
            # queue desync. Pop once saved clears `min_save_ratio` of target
            # (floored at 1 so a zero-save run never pops — that's the rc=3
            # backstop). 1.0 restores the old exact-target behavior.
            min_save_ratio = batch_cfg["min_save_ratio"]
            pop_threshold = max(1, math.ceil(target_count * min_save_ratio))
            if saved >= pop_threshold:
                # Under the dynamic model "disk" is the authority: every turn already
                # re-reads with read_queues() at the start, so real_* is the disk content at
                # this moment. reconcile_todo_with_disk is run once more before the pop to
                # keep the existing "external edit → back up the original disk content, disk
                # wins" semantics (the reconcile here is mostly a no-op, since real_* was just
                # read; but if it was changed again during the minutes generate_loop ran,
                # this reconcile catches it and backs it up).
                # Only "real queue files" are reconciled / popped: in fallback mode
                # (prompt.md / character2.md / undesired.md) the corresponding todo_*.md is an
                # empty file, necessarily different from the in-memory [fallback], so an
                # unconditional reconcile would misjudge every character as externally
                # modified and write redundant empty backups. The guard condition matches the
                # pop below.
                def _pop_cursor(path, disk_list, entry, is_fb,
                                reuse_tail=True, preserve_blank=False):
                    """Pop a single queue at the cursor position. disk_list is the disk
                    content at this moment (the real_* read_queues just read). Returns (new
                    list, whether it really popped)."""
                    if is_fb:
                        return disk_list, False
                    cur = reconcile_todo_with_disk(
                        path, disk_list, preserve_blank=preserve_blank)
                    if not cur:
                        return cur, False
                    if (reuse_tail and not _queue_consume.should_pop_at(
                            len(cur), skip, is_last)):
                        return cur, False
                    ri = _queue_consume.pop_index(len(cur), skip)
                    # front-match guard: pop only when the entry at the cursor position
                    # equals the entry just consumed; otherwise (the user reordered / deleted
                    # it) skip, without overwriting the edit.
                    if cur[ri] != entry:
                        return cur, False
                    cur.pop(ri)
                    write_todo_characters(path, cur)
                    return cur, True
                new_p, popped = _pop_cursor(TODO_PROMPT_FILE, real_p,
                                            prompt_entry, fb_p)
                if popped:
                    print(f"  popped prompt entry; {len(new_p)} remaining in "
                          f"{TODO_PROMPT_FILE.name}")
                new_1, popped = _pop_cursor(TODO_FILE_1, real_1, entry1, fb_1)
                if popped:
                    print(f"  popped char1 entry; {len(new_1)} remaining in "
                          f"{TODO_FILE_1.name}")
                # Real Character 2 queue entries are one-shot. Do not retain
                # and pad its last entry into later Character 1 batches.
                new_2, popped = _pop_cursor(
                    TODO_FILE_2, real_2, entry2, fb_2, reuse_tail=False,
                    preserve_blank=True)
                if popped:
                    print(f"  popped char2 entry; {len(new_2)} remaining in "
                          f"{TODO_FILE_2.name}")
                new_u, popped = _pop_cursor(TODO_UNDESIRED_FILE, real_u,
                                            undesired_entry, fb_u)
                if popped:
                    print(f"  popped undesired entry; {len(new_u)} remaining in "
                          f"{TODO_UNDESIRED_FILE.name}")
                # Character done & popped — drop the resume checkpoint so the
                # next run doesn't mistake a finished pair for one in progress
                # (critical when adjacent pairs share identical prompts).
                _run_progress.clear_progress()
            else:
                # Below the threshold: no pop; keep this entry at the front and move the
                # cursor past it (retried in the next run).
                print(f"  WARN: only {saved}/{target_count} saved "
                      f"(< {pop_threshold} = {min_save_ratio:.0%} threshold); "
                      f"todo entry retained for retry")
                skip += 1

            elapsed_h = (time.monotonic() - schedule_start_mono) / 3600
            print(f"  schedule elapsed: {elapsed_h:.2f}h")
            schedule_limit_h = batch_cfg["schedule_limit_hours"]
            if elapsed_h > schedule_limit_h:
                rest_h = batch_cfg["rest_hours"]
                rest_s = rest_h * 3600
                # `rest_hours: 0` is a legitimate setting (= no rest, just reset the timer).
                # A zero-length rest should not emit an event, or a "resting 0 hours" message
                # would show up on the chat platform.
                if rest_s > 0:
                    # This is deliberately **split into two variables**, not merged into
                    # one:
                    #   * `rest_started_mono` only measures "how long it actually rested" =
                    #     an interval → monotonic clock (the rest is 6 hours by default, the
                    #     most likely to straddle an NTP sync).
                    #   * `wake_ts` is an absolute point in time that **leaves this
                    #     process**: the "wakes at what time" printed for people, written into
                    #     the `schedule_rest` event for the bot's `_resting_until` to compare
                    #     with its own `time.time()` (`wake <= now` = the rest has expired).
                    #     monotonic's zero point differs in every process, so this value
                    #     would be completely meaningless as monotonic.
                    # **This is the only `time.time() + N` deliberately kept in this file.**
                    # The other seven DOM polling timeouts (`select_model` twice,
                    # `_click_gender`, `click_add_character_control`,
                    # `_click_option_by_text`, `select_sampler`, `click_generate`) have all
                    # been switched to the monotonic clock; whoever does the next sweep of
                    # this kind, please **do not** "fix" this line along with them — it is not
                    # a timeout check, it is an absolute point in time that leaves this
                    # process.
                    rest_started_mono = time.monotonic()
                    wake_ts = time.time() + rest_s
                    wake_at = time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(wake_ts))
                    print(f"  schedule limit ({schedule_limit_h}h) exceeded; "
                          f"resting {rest_h}h (wake at {wake_at})")
                    # Resting is **deliberate** idling, not a hang — but from outside the two
                    # cannot be told apart: `/rate`'s "no new image for over an hour" warning
                    # would light up for every minute of the rest. Emitting an event lets the
                    # bot explain it, and gives `/rate` / `/eta` something to go on.
                    emit_event("schedule_rest", character=char_name,
                               rest_sec=round(rest_s, 1),
                               wake_ts=round(wake_ts, 1),
                               worked_sec=round(elapsed_h * 3600, 1))
                    if rest_until(port, wake_ts):
                        prev_prompt = None
                        prev_e2 = None
                        prev_undesired = None
                    # How long it actually rested must be **measured**, not stood in for by
                    # the config value: the pause marker stretches the rest, and so do
                    # interjected single images.
                    emit_event("schedule_resumed", character=char_name,
                               rested_sec=round(
                                   time.monotonic() - rest_started_mono, 1))
                schedule_start_mono = time.monotonic()
                print("  resumed; schedule timer reset to 0")

            # `fallback_single`: the real queues are all empty and this round produces only
            # this one fallback character → once produced (already popped / cleared), wrap up
            # without re-reading (so a length-1 fallback does not stretch a drained real
            # queue into ghost characters). Placed before the chrome restart, to save a
            # pointless restart.
            if fallback_single:
                break

            # Memory-leak mitigation: cycle Chrome every N completed characters
            # to flush the renderer before it OOMs on the long run. Done at the
            # character boundary (not mid-character) so no batch is interrupted.
            # Skipped when nothing is left to generate (run about to end anyway):
            # the dynamic model has no fixed "last batch", so peek with read_queues()+decide()
            # at whether the next turn will BREAK, and if so skip the restart (restarting for
            # nothing and then tearing down wastes time).
            chars_completed += 1
            restart_every_chars = batch_cfg.get(
                "restart_chrome_every_n_characters", 0)
            more_pending = True
            if restart_every_chars > 0 and chars_completed % restart_every_chars == 0:
                _r, _e, _f = read_queues()
                _peek = _queue_consume.decide(
                    *_r, *_e, *_f, skip, produced)
                more_pending = _peek.action != _queue_consume.ACTION_BREAK
            if (more_pending and restart_every_chars > 0
                    and chars_completed % restart_every_chars == 0):
                print(f"  [memory] {chars_completed} character(s) done; cycling "
                      f"Chrome to flush renderer memory before the next one")
                emit_event("chrome_restart", character=char_name,
                           chars_completed=chars_completed)
                port.restart(email, password)
                # Fresh blank page → force the next pair to re-fill every field
                # (the per-pair diff against prev_* would otherwise skip an
                # unchanged value and leave the new session's field empty).
                prev_prompt = None
                prev_e2 = None
                prev_undesired = None
        # Safety net: a run that walked every pair but saved ZERO images is a
        # dead page that wasn't caught as a crash interstitial (logged-out,
        # un-mounted React app, etc.). Returning 0 here would tell the
        # supervisor "todo finished cleanly" and it would NOT respawn —
        # leaving the queue stuck. Emit a failure (not todo_done) and return
        # non-zero so the supervisor respawns Chrome.
        if end_sentinel_hit:
            # `end` sentinel stop is a clean finish, NOT a broken session —
            # bypass the zero-save backstop below (which would respawn) and
            # exit rc=0 even if nothing was saved this run.
            print(f"\nSTOPPED at 'end' sentinel — total saved {total_saved}")
            emit_event("todo_done",
                       total_saved=total_saved,
                       total_pairs=last_pairs_len,
                       stopped_by_end=True,
                       elapsed_sec=round(
                           time.monotonic() - schedule_start_mono, 1))
        elif produced > 0 and total_saved == 0:
            # rc=3 backstop: at least one character was actually attempted (produced>0) yet
            # 0 files were saved — a dead page not recognised as a crash interstitial (logged
            # out / React not mounted / maintenance). Returning 0 would make the supervisor
            # treat it as a "clean finish" and not respawn → the queue is stuck. Return
            # non-zero so it respawns.
            print("WARN: generated character(s) but saved 0 images — treating as "
                  "a broken session; exiting non-zero so supervisor respawns.",
                  file=sys.stderr)
            emit_event("critical_error",
                       message=f"attempted {produced} character(s) but saved 0 "
                               f"images (broken session)")
            return RC_ZERO_PROGRESS
        else:
            print(f"\nALL DONE — total saved {total_saved}")
            emit_event("todo_done",
                       total_saved=total_saved,
                       total_pairs=last_pairs_len,
                       elapsed_sec=round(
                           time.monotonic() - schedule_start_mono, 1))
    except GenerationBlockedError as error:
        # The site blocks generation and retrying will get nowhere → stop cleanly, and do
        # **not** let the supervisor respawn.
        # The event deliberately does not carry the dialog's raw text: that is the site's raw
        # string, which under the secrecy rules must not be sent to the chat platform (the bot
        # side only ever posts a fixed Chinese notice). The full text is already written to
        # stderr / the log.
        print(f"generation blocked; stopping without respawn: {error}",
              file=sys.stderr)
        emit_event("generation_blocked", saved=total_saved, produced=produced)
        return RC_GENERATION_BLOCKED
    except Exception as error:  # pylint: disable=broad-except
        # `message` goes through `_long_error`: a bare `str(error)` has no length cap, and
        # would carry chromedriver's dozen-plus lines of C++ `Stacktrace:` into the event
        # file wholesale.
        # `traceback` goes through `_traceback_excerpt`: the old `format_exc()[-1500:]` cut
        # off all of our own frames when the exception came from deep inside a library
        # (measured three times on 09-07, with not one of our frames left). `code_drift`
        # says whether the source text in that traceback can be trusted.
        emit_event("critical_error",
                   message=_long_error(error),
                   code_drift=code_drift_flag(),
                   traceback=_traceback_excerpt(traceback.format_exc()))
        # "This round attempted generation yet saved not a single image" = zero output.
        # Returning rc=3 (the same value as the full-round zero-save backstop below) rather
        # than letting the exception blow through as rc=1 is what lets the supervisor count
        # "how many rounds in a row had zero output" and give up when it should — otherwise a
        # slow failure (each round has to run through consecutive_fail_abort before dying)
        # never reaches the rapid-fail gate.
        # A crash after images were saved still blows up as before: that is the case that
        # really should respawn indefinitely.
        if produced > 0 and total_saved == 0:
            print("WARN: run produced no images at all before aborting; "
                  "exiting rc=3 so the supervisor can count zero-progress "
                  "runs.", file=sys.stderr)
            return RC_ZERO_PROGRESS
        raise
    finally:
        # Every exit path must release it: a normal finish, rc=3 / rc=4, and exceptions
        # blowing up. `release()` is idempotent and never raises, so putting it here cannot
        # mask the real cause of failure.
        _awake.release()
    return 0
