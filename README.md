# Axiomatic

**English** · [繁體中文](README.zh-TW.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md)

**A chat-operated automation host.** You talk to a machine from whatever chat
platform you happen to be in, and it does work on that machine for you:
desktop and window automation, process and file operations, scheduled jobs,
conversational answering through a pluggable backend, and a long-running
browser batch that generates images from a queue you edit by chat.

**No single platform is the identity of this project.** The chat surface is an
adapter: one supervised **process per platform**, each independently
switchable, each with its own lock, log and state. Adding a platform is one
transport module plus a config section; removing one is a single `false`.
Workloads sit behind that adapter and do not know which platform a command
arrived from.

**Who it is for**: someone who runs a machine that should keep working while
they are not at it — long unattended jobs they want to start, watch, adjust
and stop from a phone, with the host-control surface locked to one owner
identity per platform.

> **Writing convention**: prose refers to external dependencies generically
> (the image service, the target site, the web UI, the answering backend);
> file names, command names, config keys and code symbols are written out.

<!-- section: contents -->
## Contents

- [What this is](#what-this-is)
- [Requirements](#requirements)
- [Setup](#setup)
- [Running one or several platforms](#running-one-or-several-platforms)
- [Configuration files](#configuration-files)
- [Queues and prompt files](#queues-and-prompt-files)
- [Commands](#commands)
- [Batch behaviour](#batch-behaviour)
- [Supervision and restart](#supervision-and-restart)
- [Where the deeper docs are](#where-the-deeper-docs-are)
- [Development](#development)

---

<!-- section: what-this-is -->
## What this is

```
   platform A        platform B        platform C …
       │                 │                 │
       ▼                 ▼                 ▼
 ┌───────────┐     ┌───────────┐     ┌───────────┐
 │ bot proc  │     │ bot proc  │     │ bot proc  │   one process per platform
 │ own lock  │     │ own lock  │     │ own lock  │   own log, own state
 │ own state │     │ own state │     │ own state │
 └─────┬─────┘     └─────┬─────┘     └─────┬─────┘
       └─────────────────┼─────────────────┘
                         │  files on disk (the only coupling)
        ┌────────────────┼──────────────────┬───────────────────┐
        ▼                ▼                  ▼                   ▼
  desktop &        host commands,     answering backend   image batch runner
  window control   jobs, schedules    (pluggable)         (claimed by lock)
```

- A **platform process** is the intent layer: it receives commands, checks who
  is asking, edits files on disk, starts and supervises work, and reports back.
  Each one serves exactly one chat platform and shares no mutable state with
  the others.
- The **workloads** are the execution layer. Desktop automation runs in the
  platform process; the image batch runs as its own supervised child process
  that drives a browser — log in, fill the prompt, press generate, download.
- **Only files sit in between.** All shared state is on disk, so a restart
  anywhere is harmless, and the batch runner never imports the bot or the
  reverse.

The one sanctioned third path is a **passive shared module**
(`_batch_config`, `_queue_consume`, `_webrunner_shared`, …): both sides may
import those, because they are standalone, stateless, driver-agnostic code.

### Components

**The chat-platform layer** — what makes the bot platform-agnostic

| File | Purpose |
|---|---|
| `axiomatic/_chat_platform.py` | The adapter seam: identity mapping, capability flags, outbound-argument normalisation, the transport registry |
| `axiomatic/_telegram_transport.py` | One platform. Every further platform is one more `_*_transport.py` |
| `axiomatic/_platform_runtime.py` | Which platform this process serves, and where its own state, lock and log live |

**The command host** (the module name is historical; it is not platform-specific)

| File | Purpose |
|---|---|
| `axiomatic/discord_bot.py` | 13 top-level slash commands and 25 command groups (268 slash sub-commands in total), the identity gates, desktop and host control, batch supervision, answering-backend orchestration, the single-image queue |
| `axiomatic/_gui_control.py` | The desktop-automation façade (mouse, keyboard, windows, clipboard, OCR, image location) |
| `axiomatic/dorossi_backend.py` | The answering backend and its sessions — several backends behind one interface |

**The image-generation workload**

| File | Purpose |
|---|---|
| `axiomatic/webrunner_novelai.py` | Batch runner, Selenium variant (the production default) |
| `axiomatic/webrunner_je_only.py` | Batch runner, wrapper variant (fallback; `/run` tries this one first) |
| `axiomatic/_webrunner_shared.py` | The core both variants share: DOM work, pure helpers, the batch loop — driver-agnostic |

**Launchers** (repo root)

| File | Purpose |
|---|---|
| `start_platforms.py` | Start one supervised process per enabled platform |
| `start_discord_bot.py` | The supervision loop for **one** platform (`--platform <name>`) |
| `start_webrunner.py` | The supervision loop for the image batch |
| `run_batch.py` | One-shot local batch: pre-flight, print the run plan, hand over |
| `install_autostart.py` | Register/remove the Windows Task Scheduler logon tasks |

---

<!-- section: requirements -->
## Requirements

Windows with Python 3.11 or newer, a browser, and the packages in
`requirements.txt`. `requirements.txt` pins nothing — a fresh clone gets the
current release of each package — but it floors anything with a known
advisory this project can actually reach.

| Package | Used for |
|---|---|
| `selenium` | Browser driving in the Selenium variant |
| `je_web_runner` | Browser driving in the wrapper variant |
| `urllib3` | Both variants catch `ReadTimeoutError` directly (selenium does not re-export it) |
| `discord.py` | Transport and event loop for the platform that has a native slash menu |
| `aiohttp` | Outbound API calls, and the long-polling transport other platforms use |
| `psutil` | Process liveness probing and window matching (**required**) |
| `je-auto-control` | The **only** desktop-automation implementation: mouse, keyboard, windows, clipboard, OCR, image location |
| `pytesseract` | OCR wrapper for `/locate text find\|click\|wait`. The pip package alone is not enough — the engine must be installed and on PATH; without it those three reply with a generic notice instead of crashing |
| `comtypes` | Element location for `/locate ui …`. If it cannot be installed, that family replies generically and the other location methods keep working |
| `Pillow` | Builds the 2×2 mosaic for `/grid` |
| `pyfiglet` | `/fun ascii` |
| `simpleeval` | `/fun calc` — a safe evaluator, not `eval` |
| `anthropic` | The `api` answering backend (optional; without it the bot still starts and that path degrades with a notice) |
| `matplotlib` | Compatibility tests for the older token-chart helper |

The browser-automation library is imported by the two batch runners through
`sys.path`, from a **sibling checkout** or from `WEBRUNNER_PATH`:

```
<parent>/
├── Axiomatic/    ← this repo
└── WebRunner/    ← sibling checkout
```

---

<!-- section: setup -->
## Setup

### 1. Install the dependencies

```powershell
py -3 -m pip install -r requirements.txt
```

### 2. Fill in the credentials

**Credentials and configuration are never in version control.** The repo ships
templates; the first step is to copy them:

```powershell
copy auth.example.md                   auth.md
copy discord_bot_token.example.md      discord_bot_token.md
copy telegram_bot_token.example.md     telegram_bot_token.md
copy bot_config.example.json           bot_config.json
```

| File | Contents |
|---|---|
| `auth.md` | The image service login, two lines: `username: ...` / `password: ...` (split on the first colon) |
| `discord_bot_token.md` | The bot token for the platform with the native slash menu (the whole file is the token, or one `Token: xxx` line) |
| `telegram_bot_token.md` | The second platform's bot token. **Only copy this one if you want that platform** — an absent or empty file simply means that platform is off |

Never move them back into the tracked set, and stage files **individually**
(`git add path/to/file`), never `git add -A`.

### 3. Configure the bot

Edit `bot_config.json`. Two values are the minimum:

- `channel_id` — channel-scoped commands only answer in this channel;
- `owner_user_id` — your own user id. The host-control groups and `/dorossi`
  accept nobody else. Left at `0`, those commands are refused for everyone.

With neither filled in, the bot prints an explanation at startup and exits
cleanly — no traceback.

On the platform with the native slash menu, enable the **Message Content**,
**Presence** and **Server Members** privileged gateway intents in its developer
portal, or startup raises `PrivilegedIntentsRequired`.

### 4. Put something in a queue

Only one queue needs content — the other three fall back to `prompt.md`,
`character1.md`, `character2.md` and `undesired.md`. Those fallbacks are not in
version control either; copy the templates:

```powershell
copy prompt.example.md      prompt.md
copy character1.example.md  character1.md
copy undesired.example.md   undesired.md
```

You rarely create the queues by hand — `/todo char1 add <description>` writes
them for you. The format is **one entry per line**, never split on commas;
`todo_character2.md` is positional, so an empty row means "no second character
for this pair" and must be preserved.

---

<!-- section: running-one-or-several-platforms -->
## Running one or several platforms

**One platform, one process.** Each process has its own single-instance lock,
its own log file and its own state files, all under `state/<platform>/`. Two
platforms share no mutable state at all, so one crashing, restarting or being
switched off does not touch the others.

Start every enabled platform at once:

```powershell
py -3 start_platforms.py
```

See which platforms will start, which will not, and why:

```powershell
py -3 start_platforms.py --list
```

Start just one:

```powershell
py -3 start_discord_bot.py --platform telegram
```

Local batch without any bot at all:

```powershell
py -3 run_batch.py
```

> ⚠️ **Do not mix the bot's `/run` with the local launcher** — both spawn a
> batch runner and fight over the same browser profile lock.

### What it takes to run two platforms at once

1. Fill in that platform's token file (`<platform>_bot_token.md`).
2. Set `platforms.<platform>.enabled` to `true` in `bot_config.json`, and list
   that platform's `owner_user_ids` and `allowed_chat_ids` (ids **on that
   platform**, as strings).
3. Run `start_platforms.py`.

Everything else follows automatically: the state directory, the locks, the log
file and the autostart task are all named after the platform. A platform with
no credentials is **absent, not broken** — it is not started and it does not
complain on every boot; `--list` is where you ask why.

The default platform can be switched off the same way
(`platforms.discord.enabled: false`). With no section at all it counts as on,
because a fresh clone whose bot does nothing looks exactly like a
configuration that did not take effect.

### The image batch is still single

The batch is a machine-wide resource (one browser, one set of queue files, one
output folder), so it does not follow the platforms. Whichever process holds
the batch supervisor lock supervises it; the others answer batch commands with
"another process is already supervising" rather than starting a second
supervisor — two supervisors terminate and respawn each other while both sets
of logs look perfectly normal.

Queue editing is unaffected: the queues live on disk, so editing them from any
platform works. Only *who supervises the batch* is decided by the lock.

### Start automatically at logon (Windows)

```powershell
py -3 install_autostart.py --install    # idempotent
py -3 install_autostart.py --status
py -3 install_autostart.py --remove
```

This registers one task per enabled platform (`\Axiomatic\Bot-<platform>`)
plus one for the batch (`\Axiomatic\Batch`). The trigger is **logon**, not
boot: the batch needs a real interactive desktop for the browser.

---

<!-- section: configuration-files -->
## Configuration files

| File | What it holds | Tracked? |
|---|---|---|
| `batch_config.json` | Batch generation parameters: images per pair, waits, work/rest hours, periodic browser restart | Yes |
| `bot_config.json` | Channel and owner ids, roles, answering-backend tuning, the launch whitelist, `platforms.*` | No (template: `bot_config.example.json`) |
| `presence_games.json`, `presence_music.json`, `presence_rpc.json` | Local presence mapping | No (templates: `*.example.json`) |
| `bot_prompts/` | 10 plain-text prompt files (persona, self-driving loop guidance, the default style suffix for single images) | Yes |

`bot_config.json` is read **once at startup** — `/sys restart` to apply an
edit. Unknown keys are ignored with one warning line, because a typo looks
exactly like "the setting had no effect".

`batch_config.json` can be edited live with `/config set`, and the batch
re-reads it between images.

---

<!-- section: queues-and-prompt-files -->
## Queues and prompt files

| Queue | Fallback when empty |
|---|---|
| `todo_prompt.md` | `prompt.md` |
| `todo_character1.md` | `character1.md` |
| `todo_character2.md` | `character2.md` (an empty row means "no second character") |
| `todo_undesired.md` | `undesired.md` |

Pairing walks the queues together; a shorter queue is padded from its
fallback. An `end` entry in `todo_prompt.md` is a **stop marker**: the batch
finishes the pair before it and then stops cleanly, which is how you park a
long queue without deleting anything.

Readers split on line boundaries only — an entry may legitimately contain
commas, colons and brackets — and a non-breaking space is normalised to a
plain space. A writer refuses an entry containing a line boundary, because it
would read back as several entries.

---

<!-- section: commands -->
## Commands

On a platform with a native slash menu the commands are **slash commands**:
typing `/` autocompletes them and arguments are type- and range-checked before
they are sent. On a platform without one, the same commands are reached through
the text surface (see below) — one implementation, one set of permission gates. There are
**13 top-level slash commands** and **25 command groups** (37 top-level
entries; the platform limit is 100) holding **268 slash sub-commands**.

A group costs one top-level slot no matter how many sub-commands it has, so
folding low-traffic commands into groups is the only way to keep growing. Per
command detail is in [`COMMANDS.md`](COMMANDS.md), in
[`commands/`](commands/README.md), or from `/help`.

- 🔒 **Channel-scoped**: answers only in the `channel_id` channel (**the owner
  may use them anywhere**).
- 🌐 **Anywhere**: any channel the bot can see.
- 🔑 **Owner only**: commands that act on the machine the bot runs on. These
  **do not consult `user_roles`**.

> 🔑 **Host control is owner-only, always.** `/input`, `/screen`, `/win`,
> `/clip`, `/locate`, `/macro`, `/watch`, `/proc` and `/host` are gated as
> **whole groups** (so a new sub-command is protected automatically), plus
> scattered commands such as `/sys restart`. The gate runs **before** dispatch
> and **before** the role gate — with all three `user_roles` lists empty (the
> default) the role gate does nothing, so hanging desktop control off it would
> be no protection at all.

> **Every reply is generic**: no service names, no host paths, no file names,
> no PIDs, no raw exception text. Full detail goes to stderr and the log only.

### 🔒 Channel-scoped

| Command | What it does |
|---|---|
| `/eta` | Estimated finish time (stops counting at a stop marker) |
| `/latest` | Upload the most recent N images |
| `/queue` | Remaining entries per queue and the pair count that will actually run |
| `/run` | Start the batch (schedulable: in 90m / at 02:00 / cancel) |
| `/status` | Batch state and which variant is running |
| `/stop` | Stop the batch |

| Family | Sub-commands |
|---|---|
| **Generation queues** | `/todo dedupe\|duplicate\|find\|move\|shuffle\|swap`<br>`/todo char1 add\|addx3\|clear\|list\|pop\|remove`<br>`/todo char2 add\|clear\|default\|list\|pop\|remove`<br>`/todo negp add\|clear\|list\|pop\|remove`<br>`/todo prompt add\|clear\|default\|end\|insert\|list\|pop\|remove\|template\|unend` |
| **Fallbacks for empty queues** | `/preset info`<br>`/preset main append\|clear\|set`<br>`/preset neg append\|clear\|set` |
| **Batch control** | `/gen current\|image\|image_queue\|pause\|plan\|preview\|progress\|resume` |
| **Output browsing** | `/out debug_show\|history\|latest_for\|rate\|sample\|stats` |
| **Favourites** | `/fav clear\|list\|remove\|show` |
| **Run log** | `/log clear\|errors\|grep\|size\|tail` |
| **Operations and diagnostics** | `/sys audit\|backfill_paths\|cleanup_debug\|dashboard\|disk\|doctor\|git_pull\|health\|introspect_dom\|metrics\|probe_status\|restart\|undo\|update_check` |
| **Process control** | `/proc kill\|launch\|list` |
| **Batch parameters** | `/config reload\|reset\|set\|show` |
| **Screen** | `/screen all\|gif\|info\|main\|pixel\|region\|text\|window` |
| **Windows** | `/win focus\|grid\|list\|move\|pos\|snap\|state\|wait`<br>`/win layout list\|remove\|restore\|save` |
| **Keyboard and mouse** | `/input click\|hotkey\|type`<br>`/input key clear\|down\|press\|status\|up`<br>`/input mouse click\|dclick\|down\|drag\|move\|pos\|scroll\|up` |
| **Clipboard** | `/clip files\|formats\|image\|paste\|read\|set\|setimage` |
| **On-screen location** | `/locate gone\|pixel`<br>`/locate image click\|find\|wait`<br>`/locate text click\|find\|wait`<br>`/locate ui click\|find\|gone\|read\|tree\|wait` |
| **Macros** | `/macro delete\|edit\|insert\|list\|record\|rm_line\|run\|save\|show\|stop` |
| **Host commands and file transfer** | `/host get\|panic\|put`<br>`/host job clear\|eof\|list\|log\|run\|send\|stop`<br>`/host sh cd\|run\|stop` |
| **Conditional watches** | `/watch clip\|job\|list\|pixel\|port\|process\|stop\|text\|ui\|window` |
| **Timed schedules** | `/schedule add\|list\|remove\|run` |
| **Standalone supervisor** | `/launcher start\|status\|stop` |

### 🌐 Anywhere

| Command | What it does |
|---|---|
| `/booru` | Image-board search: a random hit for a tag (fuzzy allowed; a default image with no tag) |
| `/e621` | A random image from the furry-oriented board (NSFW by default; add rating:safe for SFW) |
| `/grid` | The four newest hits as a 2×2 mosaic |
| `/help` | Command help (tw / cn / en) |
| `/iqdb` | Reverse image search across boards (top hits plus a similarity %) |
| `/nsfw` | NSFW shortcut for the image-board search |
| `/safebooru` | A random image from the all-SFW board |

| Family | Sub-commands |
|---|---|
| **Answering backend** (🔑 owner only) | `/dorossi abort\|ai\|ask\|compact\|effort\|errors\|fullmode\|health\|logs\|model\|retry\|running\|status\|tokens\|workspace_clean`<br>`/dorossi allowdir add\|list\|remove`<br>`/dorossi queue clear\|detail\|failed_clear\|move\|remove\|retry_failed\|show\|undo`<br>`/dorossi session archive\|continue\|delete\|export\|list\|new\|rename\|reset\|switch` |
| **Tag tools** | `/tag autocomplete\|count\|suggest\|wiki` |
| **Fun / random** | `/fun 8ball\|ascii\|calc\|choose\|coinflip\|rand\|reverse\|roll\|rps\|timer` |
| **Encoding and utilities** | `/tool base64\|color\|hash\|qr\|say\|unbase64\|urldecode\|urlencode` |
| **Info and metadata** | `/info avatar\|channel\|ping\|server\|uptime\|version` |
| **Public data lookups** | `/web anime\|cat\|crypto\|dict\|dog\|fact\|github\|joke\|quote\|wiki\|xkcd` |

### `@bot <text>` — free questions

A mention with no sub-command is the free-question entry point. It is
**deliberately kept as a mention** rather than turned into a slash command: one
message can carry multiple lines, attachments and reply context, which an
option box cannot, and an interaction token expires long before a long round
does.

### Reactions

React ⭐ on an image the bot posted to favourite it, 🗑️ to delete the file.
Writes go through the backup mechanism, so `/sys undo` can restore them.

### Platforms without a slash menu

On a platform that has no native slash menu, the same commands are reached
through the text surface instead. That surface is documented for those
platforms only, in [`docs/platforms.md`](docs/platforms.md); where a slash menu
exists, slash commands remain the only advertised interface.

---

<!-- section: batch-behaviour -->
## Batch behaviour

1. **One full setup per start** (and after every browser restart): log in,
   apply the settings snapshot, verify the page is in the expected state.
2. **Dynamic queue consumption**: the queues are re-read for every character,
   so an edit made while a run is in flight takes effect at the next pair
   rather than at the next run.
3. **Per-pair loop**: generate up to the configured image count, downloading as
   it goes.
4. **Completion threshold**: an entry is only popped from the queue once enough
   images landed — a crash mid-pair therefore re-runs that pair instead of
   silently skipping it.
5. **Resume checkpoint**: progress is written atomically, so a kill at any
   moment resumes at the right place.
6. **Periodic browser restart**: long runs leak browser memory, so the browser
   is recycled on a schedule.
7. **Event stream**: the batch appends structured events that the bot watches
   and reports.

Return codes: `0` clean, `1` nothing to do, `2` session setup failed,
`3` zero output, `4` blocked (do not respawn), `5` setup incomplete (do not
respawn). The constants live in `axiomatic/_supervisor.py`.

---

<!-- section: supervision-and-restart -->
## Supervision and restart

Each launcher runs its child in a supervised loop with exponential backoff and
a rapid-fail give-up, and tees the child's console output into a log file.
Ctrl+C in a launcher window stops that loop cleanly.

**Single-instance protection** is an OS-held file lock, so it is released the
moment a process disappears — there is no stale-flag state to clean up:

| Lock | Guards |
|---|---|
| `state/<platform>/.<platform>.discord_bot_supervisor.lock` | A second supervisor for that platform |
| `state/<platform>/.<platform>.discord_bot.lock` | A second bot process for that platform |
| `.webrunner_supervisor.lock` | A second batch supervisor |
| `.batch_supervisor.lock` | Decides which bot process supervises the batch |
| `chrome_slot.lock` | The cross-process browser slot, so a batch and a verification run never open two browser stacks |

---

<!-- section: where-the-deeper-docs-are -->
## Where the deeper docs are

| Document | For |
|---|---|
| [`docs/`](docs/index.md) | The full manual (Sphinx / Read the Docs format) |
| [`docs/setup.md`](docs/setup.md) | First-time install, step by step |
| [`docs/config.md`](docs/config.md) | Every configuration key |
| [`docs/platforms.md`](docs/platforms.md) | Running on platforms without a slash menu |
| [`docs/workflow.md`](docs/workflow.md) | Pairing rules, fallbacks, the stop marker |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | When a run stalls, the browser crashes, login fails |
| [`COMMANDS.md`](COMMANDS.md) | The command index |
| [`commands/`](commands/README.md) | Per-group reference with arguments, ranges and permissions (generated from the command tree) |
| [`architecture.md`](architecture.md) | Layers, entry points, key flows, extension points |
| [`CLAUDE.md`](CLAUDE.md) | The always-on hard requirements for anyone editing this repo |

Build the manual locally:

```powershell
py -3 -m pip install -r docs/requirements.txt
py -3 -m sphinx -b html docs docs/_build/html
```

---

<!-- section: development -->
## Development

```powershell
py -3 -m pytest              # everything
py -3 -m pytest test/test_platform_processes.py   # one file
```

Tests live in `test/` at the repo root, not inside the package. A large part of
the suite turns the project's hard rules into static guards: module boundaries,
owner-only gating, atomic writes, explicit text encodings, Traditional-Chinese
vocabulary, and the documentation parity this README set is part of.

Before committing, at a minimum:

1. `py -3 -c "import sys; sys.path.insert(0, 'axiomatic'); import discord_bot, webrunner_novelai, webrunner_je_only; print('OK')"` prints `OK`.
2. Every new slash command appears in all the user-doc corpora, and
   `commands/*.md` is regenerated with `py -3 axiomatic/gen_command_docs.py`
   rather than hand-edited.
3. `architecture.md` is updated when the change touches what it describes.
4. Commit subjects describe what changed; files are staged individually.

The full list is the Definition of Done in [`CLAUDE.md`](CLAUDE.md).
