# Project Guidelines

Image-generation automation: a browser-driven **batch generator** plus a
**Discord bot**, coupled only through files on disk.

This file holds **only the always-on hard requirements**. Descriptive detail
lives elsewhere — keep it there:

| Need | Read |
|---|---|
| Layers, entry points, key flows, extension points | `architecture.md` |
| Command reference / usage | `README.md`, `COMMANDS.md`, `commands/`, `docs/` |
| First-time install, credentials, configuration | `docs/setup.md`, `docs/config.md` |

**The three surfaces that must stay in sync** — nothing enforces this by
construction, so it is a rule:

| Area | Files that move together |
|---|---|
| Batch generation | `webrunner_novelai.py`, `webrunner_je_only.py` (two driver variants over one shared core, `_webrunner_shared.py`) |
| Bot ↔ webrunner previews | `discord_bot.py`'s run-plan snapshot vs `_queue_consume`'s dynamic decisions |
| Slash surface | the command tree, `_help_strings.py` (three languages), `README.md`, `COMMANDS.md`, `docs/commands_*.md`, `commands/*.md` |

## Durable knowledge goes into the tree, not into a reply

A finding that the next person (or a fresh session) would otherwise re-discover
belongs in the code: the module's docstring, the constant's comment, or — when
it is a cross-cutting hard rule — this file. The test is "would someone editing
this same code hit the same trap again?" Yes → write it down next to the thing
it is about. A one-off bug that is now fixed, or the status of one task → no.
Do not pile notes at the end of a file; put them where the reader will be.

## Definition of Done

Every change MUST satisfy these before commit:

1. **Smoke-test imports**: from the repo root,
   `py -3 -c "import sys; sys.path.insert(0, 'axiomatic'); import discord_bot, webrunner_novelai, webrunner_je_only; print('OK')"`
   prints `OK` with no traceback.
2. **Slash is the only advertised surface.** Every new slash command (group
   sub-commands included) MUST appear in all five user-doc corpora: the three
   `_help_strings.py` languages, `README.md`, `COMMANDS.md`,
   `docs/commands_*.md`, and `commands/*.md`. `test_docs_sync.py` extracts the
   tree by AST and fails on both directions (undocumented command, orphan doc
   entry). `commands/*.md` is **generated** from the tree (one file per group,
   plus an index) by `axiomatic/gen_command_docs.py` — run
   `py -3 axiomatic/gen_command_docs.py` rather than hand-editing, or the next
   regeneration silently reverts the edit.
   `test_docs_sync.test_commands_docs_are_generated` compares the files against
   the generator's output, so a hand edit now fails instead of surviving until
   someone regenerates. Editorial prose that the tree cannot supply (per-group
   notes, per-command notes) lives in the generator's `NOTES` / `COMMAND_NOTES`
   dicts — put it there, never in the `.md`.
3. **`!` and `@bot` stay, unadvertised.** They remain as hidden compat paths
   for phone typing, multi-line pastes and reply context — things an option
   box cannot do. Two consequences, both guarded: a new `!` / `@bot` handler
   MUST have a slash equivalent (declared via `extras={"bang": "!x"}`), and
   **no user doc may teach `!cmd` or `@bot <sub-command>`** (the sole
   exception is the `@bot <文字>` free-question entry point). Do not
   "re-document" the text surface — that is the drift this guard exists to
   stop. The same ban applies to strings the bot sends: never tell a user to
   type `!cmd`.
4. Any new dependency added to `requirements.txt` MUST be `pip install`-ed
   during the change, so the launcher can actually start the bot afterwards.
   **`requirements.txt` pins nothing but floors what it must.** No `==` — a
   fresh clone should get the current latest. But a package with a known
   vulnerability *this project can actually reach* gets a `>=` floor plus a
   comment naming the advisory and the call site that reaches it; a floor
   blocks drifting **down**, so it does not contradict the no-pin policy.
   `test_dependency_floors.py` enforces both directions against the version
   **the running interpreter actually has**. That matters because a machine
   usually has more than one dependency set (a system interpreter, a `.venv`,
   and whatever a fresh clone resolves to), and only the one the suite runs on
   is ever measured — so run it on the interpreter you actually launch with.
   A floor that nobody measures is how a package sits fourteen advisories
   behind with the whole suite green.
5. The launcher's interpreter discovery order (local `.venv` → `py -3` →
   `sys.executable`) MUST remain intact — fresh clones depend on it.
6. Don't break the on-disk todo file format (contract below).
7. **`architecture.md` MUST be updated when a change touches what it
   describes** — the layer/module table, the entry points, the return-code or
   disk contracts, the key flows, the extension points, or a cross-project
   boundary (§6). Never deferred, never dismissed as "too small to matter"; if
   a change genuinely records nothing new, say so explicitly rather than
   skipping silently. It stays a **short overview**: per-module detail belongs
   in that module's docstring and in the test that pins it, not here. Do not
   grow it into a table of line counts — a number nobody measures rots
   silently, and a rotten number is worse than no number.
   The doc's writing convention binds too: generic terms for external services
   in prose, real names for files / symbols / config keys.

## Module boundaries (core architecture invariant)

`discord_bot.py` and the two webrunner variants communicate **only through
files on disk**. Never introduce `from webrunner_novelai import X` in the bot
or vice versa — cross-process state stays on disk so a restart is harmless.

**Permitted third channel:** passive shared modules (`_batch_config`,
`_bot_config`, `_bot_prompts`, `_queue_consume`, `_webrunner_shared`,
`_run_progress`, `_supervisor`, `_chrome_slot`, `_process_control`,
`presence_probe`, `_code_fingerprint`, `_warn_dedup`, `_power_request`,
`_connectivity`) — separate stdlib-only / driver-agnostic modules both sides may
import. That is not a bot↔webrunner import. The last two were added 2026-09-22:
`_power_request` is the one reference-counted power-request implementation (the
bot holds it while supervising a batch; the batch's `StayAwake` is to delegate to
it too, in a separate change applied only while no batch runs) — its only state
is a process-local counter, never shared across processes;
`_connectivity` is a pure "can this host reach the internet" probe both batch
supervisors use to tell a network outage from a crash. (`_external_apis`,
`_help_strings`, `dorossi_backend`, `discord_rpc` are bot-only helpers, not
boundary channels; `_gui_control` is bot-only too — the desktop-automation
façade over the external library. None of them may import `discord_bot` —
that would be circular.)

**The boundary line inside that channel:** the bot imports ONLY the pure
snapshot primitives its `/gen plan` / `/gen preview` / `/queue` / `/eta`
previews need (`pair_todos`, `character_folder_name`, `END_SENTINEL` /
`is_end_marker`, and the `ACTION_FALLBACK_SINGLE` label constant), plus one
spawn-time constant: `SINGLE_IMAGE_SERVER_FLAG`, the argv flag declaring
whether the process being spawned is a batch or a single-image server. That
one is a wire contract — the bot builds the argv, the child reads it — so a
copied literal would drift in silence: rename it and the one-shot spawn still
succeeds, the child simply no longer knows what it is and runs the whole batch
queue. Like `character_folder_name` it lives in the shared webrunner-side
module rather than the queue-consumption one, so it is deliberately absent
from the code-side allowlist constant, which is scoped to the
queue-consumption module alone. (That constant's own name is left
un-backticked on purpose: this paragraph's extractor reads every backticked
identifier as a symbol that must exist in one of the two shared modules.) The
**dynamic consumption decisions** (`_queue_consume.decide` / `simulate`) stay
webrunner-only — the bot never runs the per-character re-read logic.

All of this is now enforced statically, not by review: `test_bot_helpers`
fails if the bot imports a `webrunner_*` (or vice versa), if any package
module imports `discord_bot` (circular), or if the bot reaches for a
`_queue_consume` name outside the allowlist above. Widening the allowlist
means editing the enumeration here too — **and that is now checked, not merely
requested.** Until 2026-09-11 the only thing asking for it was a sentence in a
test's failure message, i.e. a convention: widen the constant without touching
this paragraph (or the reverse) and nothing went red, leaving the single rule of
record describing a narrower gate than the one that runs. `test_bot_helpers` now
extracts the backticked names from this very paragraph and reconciles both
enumerations against the two constants **in both directions**, the way
`_OWNER_ONLY_GROUPS` already was. Two consequences worth knowing before you edit
this sentence: the extractor keys off the literal heading
`**The boundary line inside that channel:**` and the phrase
`**dynamic consumption decisions**` (which splits allow from deny), and
`character_folder_name` is deliberately *not* in
`_BOT_ALLOWED_FROM_QUEUE_CONSUME` because it lives in `_webrunner_shared`, not
`_queue_consume` — the reconciliation intersects with each module's real public
API rather than assuming this list is single-module.

**Those four checks are all exclusions, and an exclusion cannot see a new
channel.** They forbid shapes — the bot importing a `webrunner_*`, anything
importing `discord_bot` — which is the right design for the worst cases,
because a new module falls under them automatically. But lift a chunk of logic
into a *new* passive module and import it from both sides and not one of them
fires: the architecture says disk is the only coupling and passive shared
modules are the one sanctioned exception, yet the number of exceptions could
grow as a by-product of a refactor, decided by nobody. Since 2026-09-11
`test_bot_helpers._SHARED_CHANNELS_IN_USE` pins the set that is actually shared
(measured: `_batch_config`, `_webrunner_shared`) and reconciles it both ways.
Going red is usually **not** a bug — confirm the module is stateless,
driver-agnostic pure logic, then add it here and to the list above *with its
reason*. Forcing that step is the entire point.

Note what is deliberately **not** checked: the Permitted-third-channel list
above is a **permission** list, not an as-built one, so "listed but not
currently shared" is the normal state (12 of its 14 names are not shared right
now). Comparing it by equality would pressure someone into rewriting it as
as-built, which would **delete** permission information. Only the undeclared
direction is worth a gate. **Those two numbers used to read "16 of its 18" and
both were wrong** (2026-09-11): 18 counted every backticked name in the paragraph,
including the four bot-only helpers plus `_gui_control` and `discord_bot`, which
the very next sentence says are *not* channels. Nothing was checking them, exactly
as with the `_pid_alive` numeral — so
`test_bot_helpers.test_the_permitted_channel_sentence_counts_its_own_list` now
derives both from the parenthesised list and `_SHARED_CHANNELS_IN_USE`. The list
itself is still deliberately **not** reconciled, for the reason just given; what is
checked is only that this sentence adds up. Also excluded from the comparison: the package name
itself, because `from axiomatic import _chrome_slot` — this repo's own idiom —
records both `axiomatic` and the module, and only the latter is a channel.
That exclusion is scoped, not a pass: a mutant using exactly that form to sneak
in a real shared module is still caught.

**The extractor behind those three checks was once blind to one import form.**
`_module_imports` read `ImportFrom.module` and never looked at
`ImportFrom.names`, so `from axiomatic import webrunner_novelai` recorded only
`"axiomatic"` — the module itself was invisible, and all three checks passed.
That form is not hypothetical: `start_webrunner.py` uses
exactly it (`from axiomatic import _chrome_slot`), so it is this repo's own
idiom. The `names` half is now read too, filtered to aliases that actually name
a module on disk — without that filter,
`from _supervisor import webrunner_exit_needs_human` reads as a `webrunner_*`
import and the guard cries wolf. Four forms, one canary each.

**Consequence:** the bot's run-plan preview is a point-in-time *snapshot*
while the webrunner consumes queues *dynamically*. That split is deliberate
duplication; the primitives underneath are single-sourced, so if you change a
primitive, change its one source.

## todo file format (shared on-disk contract)

Newline-separated, one entry per line. The format is settled — do not switch
to commas, JSON, or YAML. Both `read_todo_characters` (webrunner) and
`read_todo_entries` (bot) MUST:

1. Split on newlines only, never on commas. An entry can legitimately contain
   `,`, `，`, `::`, `(`, `)`. "Newlines" means everything `str.splitlines()`
   splits on — `\r`, `\v`, `\f`, `\x1c`–`\x1e`, `\x85`, U+2028 and U+2029 as
   well as `\n` — because that is what both readers call.
2. Skip empty / whitespace-only lines for prompt, Character 1, and undesired.
   **Exception:** `todo_character2.md` is positional — preserve its empty
   rows, because an empty row means remove/disable Character 2 for that pair.
   (`str.strip()` already treats `\xa0` NBSP as whitespace.)
3. Replace `\xa0` (NBSP) with a regular space inside each surviving entry.

`write_*` MUST preserve Character 2's positional empty rows, end the file with
exactly one trailing newline when non-empty, and write nothing when the list
is empty. Blank lines in the other three queues remain invalid.
A writer MUST NOT write an entry that contains any of those line boundaries:
it would read back as several entries, and in the positional Character 2
queue that shifts every later pair with no error anywhere. The bot's
`write_todo_entries` refuses such an entry before touching the file; a caller
holding free text splits it the way `add` does (`_split_add_payload`) or
refuses it.

The padding-aware pop and the `end` sentinel live in
`_queue_consume.pair_todos` / `decide` on the webrunner side, mirrored by the
bot's run-plan preview; both are documented in those functions' docstrings.

## Host control is owner-only (HARD REQUIREMENT)

Commands that act on **the machine the bot runs on** are gated to
`OWNER_USER_ID` **before dispatch, independently of `user_roles`**.

**Why not the role system:** `_roles_configured()` returns False when all three
`user_roles` lists are empty — which is the default, so it is what most
installs are running. The role gate then does nothing, so "can post in the configured
channel" would equal "can type on the keyboard, screenshot the desktop, read
the clipboard and kill processes". A protection that is off by default is not
a protection.

**The rule is group-based, so it is fail-closed:** `_OWNER_ONLY_GROUPS`
(`input`, `screen`, `win`, `clip`, `locate`, `macro`, `watch`, `proc`, `host`,
`schedule`, `launcher`) covers every current AND future sub-command of those groups
automatically. Scattered individual commands go in `_OWNER_ONLY_SLASH`. Never
replace the group rule with an enumerated list of commands — that is how a new
sub-command ends up silently unprotected.

**That list above is itself reconciled, because it had already gone stale.**
`schedule` was promoted from `_OWNER_ONLY_SLASH` to a group rule on 2026-09-10
and this sentence still named nine groups — a rule-of-record file describing a
gate one member short, with no symptom. `test_bot_helpers` now compares the
names in these parentheses against `_OWNER_ONLY_GROUPS` **in both directions**,
so adding a group means editing this line too and the test says so.

**The reverse direction has its own rule: a group whose every sub-command sits
in `_OWNER_ONLY_SLASH` should BE a group rule.** Enumeration fails **open**, so
that shape is a gate waiting to lose its next sub-command;
`test_a_fully_enumerated_group_should_be_a_group_rule` detects it. A
reconciliation that checks only one direction leaves the other permanently
green.

**That enumerated exception fails OPEN, so it needs its own reconciliation.**
Rename a command, or move it into another group, and the entry in
`_OWNER_ONLY_SLASH` becomes a string that never matches anything again — the
command loses its owner gate with no symptom at all: the gate still runs, the
set is still there, every test stays green. Nothing checked it until
2026-09-09; measured then, all 109 host-control tests passed with a
deliberately stale entry. `_OWNER_ONLY_GROUPS` has the same shape (a renamed
group), so both are now reconciled against the AST-extracted tree.

**All three surfaces must be gated, and they use different identifiers:**
slash (`qualified_name`, in `tree.interaction_check`), `!` (`head` plus every
dispatcher alias, in `on_message`), and mention (`head_lower`, in
`_handle_mention` — note that path runs **before** the channel gate and had no
gate at all, which is how `@bot restart` was once callable by anyone in any
server). Each gate sits **before** the role gate.

`test_bot_helpers.py` pins all four sets, each in both directions, all against
the tree extracted by AST: every entry of `_OWNER_ONLY_SLASH` and
`_OWNER_ONLY_GROUPS` must still name a real command / group; every alias of a
locked `!` command must be in `_OWNER_ONLY_BANGS` and nothing stale may sit
there; the mention entries must exist and none may be stale; and gate ordering
is checked on the AST (not on the source text — the gate's own comment
mentions the role gate).

**A reconciliation test needs a positive control, because an empty extraction
looks exactly like a clean result.** Each of those tests asserts a floor on
both sides first (the tree yielded ≥250 commands / ≥20 groups, the lock list
is non-empty) before asserting there is nothing stale. And because a clean
list makes the real-data assertion untestable — deleting it goes green — the
comparison itself lives in one helper with its own synthetic control test.

## Secrecy (HARD REQUIREMENT)

Two layers, both binding.

### Layer 1 — strings the bot sends to Discord

**Every** Discord-sent string on **every** surface (`!`, `/`, `@bot`; ack,
result, error, status) — **including inside `CHANNEL_ID`** and for owner-only
cross-channel commands — MUST NOT contain:

- the **image source / any external service or third-party API** the stack
  uses (image service, booru/web APIs, LLM provider), the user-facing noun
  "webrunner", or wording revealing this is an image-generation pipeline;
- **host-local paths**, absolute OR project-relative (`output/…`, `*.log`,
  `todo_*.md`, `.chrome_profile/`, `*_config.json`), credentials, or PIDs;
- **raw uncontrolled internal strings** — above all verbatim exception text
  (`str(error)` / `repr(error)`), which can smuggle any of the above through.

Instead send a **generic** message (「產圖失敗，請稍後再試或查看 log。」) and
write full detail to **stderr / the log file** only. Strings the bot authored
and that are known-safe by construction (a fixed status line, a size in MB, a
generic "done") are fine; anything the bot does not fully control is not.
Status replies map on-disk files to generic labels via `_list_label()` rather
than echoing `path.name`.

**One exemption carries a lot of weight, so it has its own rule.**
`test_secrecy._SAFE_EXCEPTION_TYPES` lets a send site interpolate a caught
`GuiError` / `GuiAborted` verbatim — that is why the GUI-control commands may
reply with the exception text directly. The exemption is sound **only because
every one of those messages is a generic sentence this project wrote**, which
is a promise made at the `raise`, not at the send. `GuiError` has no
constructor to enforce it, and for a long time nothing checked it: the
exemption covered 182 raise sites (174 in `_gui_control.py`, 8 in
`discord_bot.py`) while the secrecy scan only ever read `discord_bot.py`'s send
sites and the help corpora (measured when that gap was found). So: **never put a caught non-`GuiError` exception,
a host path, or an external service / driver / OCR-engine name into a
`GuiError` message.** Composing one safe message inside another
(`except GuiError as error: raise GuiError(f"第 N 行：{error}")`) is fine and is
the established idiom. The same three rules now run at the raise sites, reusing
the same scanner rather than a second copy; a module that starts raising the
family must be added to `_GUI_ERROR_RAISERS`, which is reconciled both ways.


**擁有者例外。** 上面整段**只適用於非擁有者**。當**提問者**是 `OWNER_USER_ID`
（`bot_config.json` 的 `owner_user_id`）時，所有限制解除：原始例外文字、主機
路徑、PID、真實檔名、外部服務名一律照實送出。這條的前提是「擁有者就是這台機器
的人，對他沒有什麼好隱藏」；把它拿掉會讓擁有者自己也查不到問題。

實作上這是**單一決策點**：`discord_bot._owner_detail(source, raw, generic)`，
配套的 `_asker_id()` / `_owner_unrestricted()` 也在同一處。任何要在「原始／泛用」
之間二選一的送出點都走它，**不要**在別處自己再寫一次 `== OWNER_USER_ID`。
`_list_label(path, source)` 與 `_paths_visible_here(message)` 已經內建這條。

**這是身分閘，不是表面閘**——依據是「誰問的」，不是「訊息貼在哪個頻道」。已知
且刻意接受的取捨：擁有者在公開頻道下指令時，同頻道其他人也看得到完整細節。
**不要把它「修正」回表面閘**，那是在推翻一個刻意的決定。
`_paths_visible_here()` 原本的表面條件（私訊、`path_reveal_channel_ids`）保留，
供非擁有者使用。

取不到提問者 UID 時一律當作非擁有者（fail-closed）。`owner_user_id` 沒設定（0）
時沒有人是擁有者，整條例外等於關閉——那是對的預設。

help 語料是靜態字串、沒有提問者可判定，所以照舊一律泛用化；唯一存留的例外是
語言那一條（zh-CN 段落維持簡體）。

**窄範圍例外（功能面）**：`/model` 微調指令的**功能面**
（help 合法值清單、無效值提示、`/dorossi session list` 的模型顯示）可露出後端
模型**別名**（`DOROSSI_MODEL_CHOICES` 的 key），由 `_model_alias_for()` 把不認得
的值降級成泛用字串。注意 `/effort`・`/model` **不是斜線指令**，是打在
`@bot <文字>` 開頭的微調 token，所以指令樹的守門掃不到它們。**僅此一處**，不得
外推——CLI 佈線、路徑、原始錯誤、其他服務名照舊全禁。後端 id 本身
（`claude_code` / `codex` / `api`）不在例外內，對外一律走 `_backend_display()`
的中性代號。

**既有指令名例外**：少數**既有公開斜線指令的名稱本身**就是外部圖庫服務名
（例如以圖庫命名的搜圖指令），以及 `/dorossi ai` 的合法值（後端別名，與上一條
同性質）。這些**維持現狀不改名**——改名是使用者面的破壞性變更，代價大於收益。
實作上這些字串列在
`test/test_secrecy.py` 的 `_ALLOWED_COMMAND_NAME_WORDS` /
`_ALLOWED_BACKEND_ALIASES`，每一筆都要寫理由。**這是清單制，不是通則**：新增
指令時不得再拿服務名當名稱，也不得用這條去合理化其他地方出現服務名。

**豁免的是「形狀」，不是那個字。** 這兩份清單一度**完全沒有作用**：
`_banned_words_in` 用的是 `hits - 允許清單`，而 `hits ⊆ _BANNED_WORDS`，那幾個字
一個都不在 `_BANNED_WORDS` 裡——減法是 no-op，圖庫指令名與後端別名從來沒有被掃描
過，而「有一份寫滿理由的豁免清單」讀起來像這件事有人在管。現在那些字真的在
`_BANNED_WORDS` 裡，豁免改由 `_strip_sanctioned` 按形狀放行：**指令引用**
（`/booru`）與**宣告裡的 `name=`** 對應「既有指令名」那條，**角括號合法值列舉**
（`<claude|codex>`，且每個選項都得是列冊過的別名）對應「窄範圍」那條。於是
「圖片來自 booru」這種泛稱服務的句子會被抓到，而它在舊寫法下永遠不會。
`test_every_sanctioned_word_is_actually_a_banned_word` 釘住這個前提——能被豁免的字
必須先是被禁的字；另外三支反查「這個字還是不是真的指令名／合法值」，因為指令改名
之後那個豁免會繼續生效而沒有任何症狀（與 `_OWNER_ONLY_SLASH` 同一個形狀）。

**同一個缺陷會分兩半出現。** 「窄範圍」那條放行的其實是**兩種**值——`/dorossi ai`
的**後端**別名，以及 `/model` 的**模型**別名（`DOROSSI_MODEL_CHOICES` 的 key）。
只把其中一半放進 `_BANNED_WORDS` 的話，「僅此一處，不得外推」對另一半完全沒有執行
力：在 help 語料裡寫「這題交給 <某模型別名>」不會被抓到。兩種現在都在
`_BANNED_WORDS` 裡，模型那半對應的豁免是 `_ALLOWED_MODEL_ALIASES`。
**兩份別名清單刻意不取聯集**——`<claude|opus>` 這種混合列舉不放行，因為那兩條各自
涵蓋的是各自那個功能面的合法值；`_strip_sanctioned` 比對的是「列舉裡**被禁的那些**
選項是否落在**同一份**清單內」，所以 `default` 這種非禁字選項可以自然地一起列出來
而不影響豁免。

**規則與既有功能衝突、還沒決定怎麼處置的，另列一份。** 送出掃描會跟著「先存進變數
再送出」走一步——`usage = "…"` 再送出 `usage` 的寫法，對三條字面規則（主機路徑、
外部服務名、教 `!`）本來是完全隱形的。跟上之後會浮出**功能本身**的命中：`/booru`
每次成功回覆都附圖庫的來源頁連結，而「既有指令名」那條例外只涵蓋指令**名稱**。
那不是清理能解決的事，所以列在 `test_secrecy._PENDING_OWNER_DECISIONS`；它按文字
**形狀**比對（禁字＋字面值開頭）而不是行號，並且兩向對帳——決定之後或程式改掉之
後，那一筆對不上任何送出字串就會紅，逼人把它刪掉。**不要**拿它收容新功能的服務名：
新東西照上面的規則寫泛用說法，這份清單只收「規則與既有功能衝突、等人決定」的那
一種。

**完整路徑的表面例外**：Dorossi 的**工作目錄**顯示
（`/dorossi session list`、`/dorossi allowdir list`／`add`、session export 附件）
可在「可露路徑的表面」印出完整主機路徑：**1:1 私訊**，或 `bot_config.json` 的
`path_reveal_channel_ids` 列出的頻道。唯一判準是 `_paths_visible_here()`，所有
目錄送出點都走 `_dorossi_dir_display()`——不要在別處自己再寫一次
`isinstance(..., DMChannel)`，那正是這個單一決策點要防的分叉。清單預設空＝回到
私訊限定；取不到 channel／id 一律 fail-closed。**這是表面閘不是身分閘**：列進去
的頻道，同頻道的其他人也看得到那些路徑，所以只放你自己控制的頻道。例外的範圍是
**那個頻道**，不是單一功能。但**其他頻道、其他表面**的主機路徑、佇列檔名、log、
原始例外文字照舊全面禁止外送，也不得拿這條去放寬憑證與原始錯誤——那兩類在任何
表面都禁止。

### Layer 2 — the Dorossi backend's replies to Discord

Dorossi 可以在**任何頻道、對任何提問者**自由說明這套系統：它是什麼、bot 與
webrunner 的內部結構、整合了哪些外部服務與平台、檔名與主機路徑、它自己怎麼接線
（後端 CLI、工具模式、工作目錄、session 儲存、watchdog 上限）、以及這一輪改了哪些
檔案。被問「你是什麼專案／跑在哪／看得到什麼／你怎麼運作」→ 照實回答。

這一層之所以比 Layer 1 寬，是因為它回答的是**關於自己的問題**，而那些答案在
repo 裡本來就是公開的；Layer 1 管的是 bot 主動送出的狀態與錯誤訊息，那裡面裝的
是這台機器當下的內容。

**唯一的硬界線：不得送出實際憑證內容**——`auth.md`、`discord_bot_token.md` 裡的
token／密碼／API key 本身。可以講這些檔案存在、叫什麼名字、放在哪，但不要把值
印出來。理由不是保密立場，是**不可逆**：這一層的對象是任何頻道的任何人，token
一旦貼進聊天室就等同帳號被接管，而且刪訊息救不回來。

**Boundary:** 本層只約束 Dorossi **送到 Discord 的回覆**。在 "full" 模式下指派的
開發工作（包含編輯本檔）不受影響——那是在 repo 裡做事，不是往 Discord 洩漏。

## Language (HARD REQUIREMENT)

All Chinese in this project — chat answers, code comments, docstrings, README
text, the zh-TW help strings, git commit subjects — MUST use **Traditional
Chinese vocabulary**, not merely Traditional characters with Mainland word
choices. Mixed-style output reads as wrong.

用戶→**使用者**、文件(file)→**檔案**、程序(program)→**程式**、進程(OS
process)→**行程**、運行→**執行**、數據→**資料**、設置→**設定**、默認→**預設**、
信息→**訊息／資訊**、網絡→**網路**、服務器→**伺服器**、內存→**記憶體**、
優化→**最佳化**、線程→**執行緒**、源碼→**原始碼**、視頻→**影片**、
軟件→**軟體**、緩存→**快取**、隊列→**佇列**、鏈接→**連結**、字符串→**字串**、
點擊→**點選／按一下**、通過→**透過**、屏幕→**螢幕**、打印→**列印**。

`程序` is technically valid Traditional for "OS process", but readers often
parse it as Mainland "program". **Default to `行程`** to keep `程式` (program)
and `行程` (OS process) cleanly disambiguated.

**Exception:** the **zh-CN section** of `CHANNEL_HELP_SECTIONS` /
`MENTION_HELP_SECTIONS` in `_help_strings.py` is intentionally Simplified (for
Mainland users) and MUST stay Simplified. Don't "correct" it.

Both directions are guarded now. `test_docs_sync` covers the zh-CN side (a
Simplified section must not drift back to Traditional); `test_language.py`
covers this side — it scans the project's own `.py` and `.md` (queue/prompt
**data** files excluded) and exempts only three mechanical cases: the word
quoted inside backticks, a line that also names the prescribed replacement
(widened to ±1 line **only for rule-table rows**, i.e. lines carrying `→`,
because the table wraps), and the `*_ZH_CN` constants. The word list deliberately omits `通過`, `文件`
and `程序`: all three have legitimate Taiwanese uses, and a guard that cries
wolf is a guard someone switches off. Those three still need a human reader.
Adding a word to the guard means adding it to the table above first — a test
enforces that direction too, so the rule of record stays this file.

**That second exemption used to be a ±1-line window for every line, and it
leaked**. The window exists because the table above wraps
(`進程(OS` / `process)→**行程**`), but it was applied to prose too, so a mainland
word was excused whenever a *neighbouring* line happened to name the
replacement. Measured: four lines passed only because of the window — three
wrapped table rows, all carrying `→`, and one ordinary prose sentence that did
not. This paragraph used to say "a line", so the rule of
record described a narrower exemption than the one that ran, in the fail-open
direction. The window is now gated on `→`
(`test_language._replacement_window`). Both near-misses are pinned: a prose
line excused by its neighbour must be caught, and a wrapped table row must
still pass — the window is a loosening step, so only the must-allow case kills
the mutant that deletes it.

**那兩道守門合起來還留著第三個方向：繁體文字裡混進來的簡體「字」**（2026-09-18
補上）。`test_docs_sync` 管「zh-CN 段落不得漂回繁體」，上面這段管「繁體文字不得用
大陸**詞彙**」——但後者比對的是**詞**，而那份詞表兩側都是繁體字，所以把字一起換掉
的寫法（`優化` 寫成 `优化`）反而穿得過去，因為它不在詞表裡。

判定住在 `axiomatic/audit_simplified_chars.py`（也可以單獨執行，結束碼 0／1／2），
閘門是 `test_language` 底下那四支。**它不 import 任何第三方套件，這是刻意的**：第一版
`import opencc` 當場撞上
`test_bot_helpers.test_every_third_party_import_is_declared_in_requirements`，而那條守門
是對的——宣告進 `requirements.txt` 會讓每個 fresh clone 都裝一個只有這支用得到的套件，
開豁免則違反「可選相依漏宣告會讓 fresh clone 安靜地少一組功能」那條理由，只當手動
工具又會因為 `.venv` 沒裝而變成永遠跳過。改成**內嵌字表**之後三個問題一次解掉，兩個
直譯器行為一致，fresh clone 一毛錢都不必付。字表是固定資料，簡繁對應不會漂。

三件事在改它之前要先知道，否則會重走一遍那四次迭代：字表**必須**用 OpenCC 的
`s2tw` 產生而不是 `s2t`
——後者轉的是「正統繁體」，連 `台→臺`、`吃→喫` 這種台灣本來就這樣寫的異體字也照轉，
實測 2,521 筆命中裡 2,405 筆是這個原因；`s2tw` 之後仍有九個字要手工排除，列在
`AMBIGUOUS` 並各自寫了理由，與上面詞表刻意不收「通過／文件／程序」是同一個原則；
刻意保留的簡體（zh-CN 語料、使用者真的會打的指令別名、繁簡對照表）列管在
`DELIBERATE`，比對用「檔名結尾 ＋ 子字串」而不是行號，而且過期的條目會被報成
stale——一個永遠對不上的豁免會安靜失效，而守門看起來照常在跑。

## Windows PID liveness (cross-cutting HARD REQUIREMENT)

**Never use `os.kill(pid, 0)` as a liveness probe on Windows.**
`signal.CTRL_C_EVENT == 0`, so signal 0 takes CPython's
`GenerateConsoleCtrlEvent` branch — it treats `pid` as a *console process
group id*, not a process id, and (verified on Windows 11 / CPython 3.14) is
wrong in **both** directions: a recently-exited child reads "alive", while a
live process outside this console group raises and reads "dead". `os.kill`
itself essentially never raises `ProcessLookupError` on Windows, so an
`except ProcessLookupError` guarding a *signal-based* probe is dead code
there.

**That clause used to read "any `except ProcessLookupError` branch is dead code
there", and the unqualified version was wrong.** It holds
for `os.kill`, and for `subprocess.Popen` (whose `send_signal` returns early once
`returncode` is set). It does **not** hold for `asyncio` subprocesses:
`Process.kill()` goes through `BaseSubprocessTransport.kill()`, whose
`_check_proc` helper raises `ProcessLookupError` outright once its `_proc`
attribute has been cleared — and `_call_connection_lost` clears it as soon as the
child is reaped. No signal and no platform test is involved, so it fires on
Windows exactly as on POSIX. Measured (Windows 11 / CPython 3.14): spawn an
`asyncio` child, await it, let the loop turn, then call `kill()` twice —
`ProcessLookupError` both times, with `transport._proc is None`. **Thirteen of
this repo's sixteen `except ProcessLookupError` handlers sit on `asyncio`
subprocesses**, so the unqualified reading was an invitation to delete live code
with no symptom at all: that handler only ever runs when the child exits in the
same instant the watchdog fires, which is precisely the race it exists for.

Enforced rather than believed:
`test_dorossi_round_outcome.test_every_asyncio_subprocess_kill_can_tolerate_a_vanished_process`
**derives** the call sites — every zero-argument `.kill()` whose receiver was
bound from `asyncio.create_subprocess_*` in the same function — so a new one is
enrolled without anyone remembering to. It deliberately does not look at
`os.kill(pid, 0)` (two arguments, opposite rule), or the guard would cry wolf on
the `_pid_alive` copies above. Sites the derivation structurally cannot see —
the process arrives as a parameter or an attribute — are listed by name in that
module's `_INDIRECT_KILL_SITES` and reconciled in both directions, the same
carve-out shape as `stream_child` / `run_full` under the encoding rule. One real
gap fell out of it the day it was written (`presence_probe`'s SMTC probe, whose
timeout branch killed the child unguarded) and is fixed.

Use `psutil.pid_exists` (psutil is a required dependency), falling back to
ctypes `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` +
`GetExitCodeProcess` with explicit `argtypes`/`restype` (a 64-bit HANDLE
truncates under the default `c_int` — and a truncated handle is still
non-zero, so `if not handle` does not catch it and the whole probe silently
degrades to "always alive"). There are three `_pid_alive` copies —
`_process_control.py`, `_chrome_slot.py`, `verify_browser.py` — and a fourth
must satisfy the same rule. Two of them take the ctypes fallback;
`_chrome_slot` deliberately does **not** — with no psutil it declines to probe
at all and returns True, leaving staleness to its time backstop. That is also
compliant: what the rule forbids is letting signal 0 reach Windows, not any
particular probe.

**The copies deliberately differ on the "can't tell" answer, and the divergence
lives on the POSIX branch** (`except OSError` around `os.kill(pid, 0)`):
`_process_control` maps an undecidable probe to **False**, `_chrome_slot` and
`verify_browser` to **True**. On Windows all three are conservatively True.
Choose by which way the mistake should fall — anything asking "should I refuse
to start / stand aside?" needs the conservative (True) variant, or it will
optimistically open a second Chrome stack.

None of this used to be checked; `test/test_pid_liveness.py` now does.
It fails on an `os.kill(x, 0)` that no `if os.name == "nt": … return` guards,
on a `_pid_alive` with no Windows path at all, on a ctypes fallback missing its
`argtypes`/`restype` **or pinning a HANDLE to a type narrower than a pointer**,
on a "harmonised" undecidable answer, and on a fourth copy that has not been
added to the enumeration above.

**That "narrower than a pointer" clause exists because the presence-only check
was satisfied by the exact defect this rule names.** The guard asserted that the
string `OpenProcess.restype =` appears — so pinning it to `ctypes.c_int`, *which
is ctypes' own default and the value the rule's own prose blames*, passed. It is
not a hypothetical either way: HANDLE is 8 bytes on 64-bit Windows and `c_int`
is 4, and the same truncation exists on the way **in**
(`GetExitCodeProcess.argtypes[0]`, `CloseHandle.argtypes[0]`), which nothing
looked at at all.

**And the static half is not redundant with the behavioural half — measured.**
Mutating `restype` to `c_int` in the tree leaves **every** behavioural test
green, because handle values are usually small enough that truncating them
changes no digit. So the wrong-width pin is visible *only* statically,
while "the probe answers `True` for a process that has exited" is visible *only*
behaviourally. The file does both: it derives the set of copies
that actually have a ctypes fallback from `_DOCUMENTED_COPIES` (so a fourth one
is enrolled without anyone remembering to) and runs each one against a live pid,
a fully-released dead pid, and **a pid that has exited while its handle is still
held** — that last one is the only input that reaches the `STILL_ACTIVE`
comparison, and it is the normal state for a supervisor holding a `Popen` and
asking whether its child is still running. Before that, `return code.value ==
259` could be replaced by `return True` in both copies with nothing going red.

**It also checks *these two paragraphs*.** Everything above
was checked against the code; the two enumerations *here* were only transcribed
into `test_pid_liveness._DOCUMENTED_COPIES` / `_UNDECIDABLE_POSIX_ANSWER`, and
the transcription had never been compared with the original — so editing these
sentences (or the transcription, without the sentences) was completely silent.
That matters most for the undecidable-answer divergence: the guard against
"harmonising" it looks at the **code**, but whoever writes a fourth copy picks
True or False by reading **this paragraph**. Both enumerations are now
reconciled in both directions, the numeral in "There are three" is checked
against the list it introduces, and every module named here must exist on disk.
Two consequences before you edit: the extractor keys off the literal phrases
`` There are <numeral> `_pid_alive` copies — `` … `— and a fourth` and
`**The copies deliberately differ on the "can't tell" answer` … `On Windows all
three`, and it reads the divergence by splitting on the bolded **False** /
**True** — so keep those markers bold and keep the False side first.

## Atomic writes for cross-process files (cross-cutting HARD REQUIREMENT)

Every file one process writes while another polls or re-reads it MUST be
written atomically (same-directory temp → `os.replace`), never
truncate-then-write. Enforced by `test/test_atomic_writes.py`.

**The covered set, by constant name** — `BATCH_CONFIG_FILE`,
`WEBRUNNER_PAUSE_FILE`, `DOM_REQUEST_FILE`, `WEBRUNNER_PID_FILE`,
`BATCH_LABEL_FILE`, `SCHEDULE_FILE`, `PROGRESS_FILE`,
`SINGLE_IMAGE_REQUEST_FILE`, `EVENTS_FILE`, `AUDIT_FILE`, `FAVORITES_FILE`,
`RECENT_IMAGE_MSGS_FILE`, `SCHEDULED_RUN_FILE`, `NETWORK_RESUME_FILE`,
`DOROSSI_MODEL_CATALOG_FILE`, `DOROSSI_QUEUE_FILE`.
Adding a cross-process file
means adding it here **and** to `test_atomic_writes._CROSS_PROCESS_CONSTANTS`;
the two are reconciled in both directions, so editing one alone goes red. The extractor keys off the literal heading above and reads the
backticked ALL-CAPS names up to the next blank line, so keep that shape.

**This paragraph once named eight things in prose while the guard covered
twelve, and the worst omission was `SINGLE_IMAGE_REQUEST_FILE`.** That one
is the textbook case for this rule — the bot writes it, the **webrunner** reads
and unlinks it — so the rule of record was missing its own best example, and a
reader who saw only queues / pid / config files could reasonably conclude that a
*request* file does not count. Same failure shape as `_OWNER_ONLY_SLASH` and the
`_pid_alive` enumeration: the guard keeps running off the constant, so a wrong rule
of record has **no symptom at all** — nothing goes red, because nothing was
comparing them. This exact pair of lists has already cost this project once: the
`.tmp`-sibling check used to keep its own hand-typed copy, drifted three constants
behind, and left four `.tmp` files un-ignored; that one was fixed by *deriving* it
from the constant. Prose cannot derive, so it gets reconciled instead.

Two entries need their reason stated or someone will tidy them out.
`EVENTS_FILE` / `AUDIT_FILE` are append-only in normal use; they are here because
their *rotation* (`_rotate_ndjson_tail`) rewrites the whole file, which is exactly
the truncate-then-write this rule forbids. `FAVORITES_FILE` /
`RECENT_IMAGE_MSGS_FILE` / `SCHEDULED_RUN_FILE` / `NETWORK_RESUME_FILE` /
`DOROSSI_MODEL_CATALOG_FILE` have a
single writer and a single reader, both the bot, so they are not cross-*process*
at all — they are here for durability across a bot restart, because a
half-written one read at startup is silent data loss (for `SCHEDULED_RUN_FILE`, a
`/run in` / `/run at` the user was told is scheduled; for `NETWORK_RESUME_FILE`, a
batch parked by a network outage that nobody stopped but that would never
resume; for `DOROSSI_MODEL_CATALOG_FILE`, the discovered model catalogue — a
half-written one reads as "never checked", which silently drops back to the
built-in table **and** re-runs the daily probe, and the only visible symptom is
that a model offered yesterday is missing today). **The set is deliberately wider
than this section's title**, and that is not drift.

**The guard also follows writes made through a parameter.** A name-based scan
only sees `CONST.write_text(...)`; it cannot see `f(CONST)` where `f` writes
`param.write_bytes(...)`. That is exactly how the `events.ndjson` rotation
stayed unchecked — the call site names the constant, the write names the
parameter, and neither line alone looks wrong. Both directions are scanned now;
adding a cross-process file means adding it to `_CROSS_PROCESS_CONSTANTS`.

**The scan covers every project module, and that list is reconciled.** The scan
once ran over a hardcoded six-tuple of modules, while this rule
names none — so `webrunner.pid`, one of the misses that prompted the guard, was
never actually checked: its only writer is `start_webrunner.py`, in the repo
root, which the six-tuple did not include. It happened to be atomic, so a wide
scan and a narrow one looked identical. The scope now has its own pin (every
module that defines one of these constants must be inside the scanned set), and
the constant list has a staleness check — rename `EVENTS_FILE` and the entry
becomes a string that matches nothing, with the guard still running and every
test still green, exactly as documented for `_OWNER_ONLY_SLASH` above.

**One deliberate exception: the todo queues.** `write_todo_characters` rewrites
them in place on purpose. Users keep `todo_prompt.md` open in an editor to watch
the queue drain, and `os.replace` swaps the inode — the editor reads that as
"deleted and recreated" and pops a reload dialog on *every* pop, while an
in-place rewrite reloads silently. The trade is real but small: the files are
tiny, the write is sub-millisecond, and bot-side edits are already protected by
`.backup/` + reconcile; the thing that actually must survive a kill is the
resume checkpoint, which stays atomic. Do not "fix" this one — the reasoning
lives in that function's docstring and the guard's exception list.

The punishment is not a crash but a **silent wrong result**: a half-written
queue file reads as empty and ends the batch cleanly with rc=0; a half-written
`batch_config.json` makes the loader fall back to the **entire** default
config (not just the edited key) while the user is told the edit succeeded.
Templates: `_run_progress._atomic_write`,
`_batch_config._atomic_write_config`, `discord_bot._atomic_write_text`.

## Text I/O always names its encoding (cross-cutting HARD REQUIREMENT)

**Never let text cross a boundary on the platform's locale codec.** Every
`open()`, `Path.read_text()` / `write_text()`, and every `subprocess` call with
`text=` / `universal_newlines=` MUST pass `encoding="utf-8"` explicitly. When
the bytes come from outside the project (a subprocess's output, a third-party
response) add `errors="replace"` too.

**Why this project in particular:** almost everything it reads is Chinese — the
todo queues, the prompt files, `webrunner.log`, git commit subjects, subprocess
output — and on a Traditional-Chinese Windows install
`locale.getpreferredencoding(False)` is **cp950**. Python 3.14 still defaults
text I/O to the locale codec, so an omitted `encoding=` is literally "decode
UTF-8 as cp950". Whatever your locale is, the rule is the same: name the
encoding, because the person who hits this will not be you.

Both failure modes are hard to trace. Illegal bytes raise `UnicodeDecodeError`,
which the nearest `except Exception` usually swallows into a generic message —
`mcmd_version` did exactly that, turning any non-ASCII git output into
"git query failed". Legal-but-different bytes are worse: they decode silently
into mojibake and get **written back to disk**, corrupting data that nobody
notices until a human reads it.

It is also invisible to most people: a developer on an English locale gets
cp1252 and sees nothing wrong. So this can only be guarded statically:
`test/test_text_encoding.py` scans every project `.py`.

Its scan is **deliberately narrow** — bare `open()` in text mode, `.open()`
with a visible text-mode literal, `read_text`/`write_text` whose receiver is
not an imported module, and `subprocess` with a text flag. A wider version had
four false positives out of six hits (`gui.read_text()` is this project's own
OCR helper; `fake_grab.open(dest)` is a test double). Same reasoning as
`test_language.py`: a guard that cries wolf is a guard someone switches off.
`X.open()` with no visible mode is knowingly left to human review.

**The one shape left to human review has a tool, and it came back clean.**
`X.open()` with no visible mode is deliberately outside the scan
above, because the receiver is unknowable statically — that is why a wider
version had four false positives out of six hits. **pylint's `W1514` resolves
the receiver by inference**, so it covers exactly that gap, including
`Path(...).open()` and `Path(...).read_text()`. Measured across every project
`.py` plus the repo-root scripts: **0 findings**, with a positive control
proving the scan was live (a synthetic file with bare `open()`,
`Path(...).open()` and `Path(...).read_text()` produced all three warnings).
Every `X.open()` in the tree that omits `encoding=` was hand-checked and is
binary mode (`"rb"` / `"wb"`), where omitting it is correct.

Two things were worth knowing before reaching for a linter here instead.
**The first was "ruff's `PLW1514` is not a substitute". That half is now false.**
It was true when written: the rule did not look at
`pathlib.Path.open()`, the very shape this gap is about. `astral-sh/ruff` #11288
added `Path.open` / `read_text` / `write_text`, and the indirect receiver
(`def f(p: Path): p.open()`) is covered too — the one the issue tracker still
lists as a false negative. Measured on ruff 0.15/0.16 (identical answers): a
five-shape positive control scored **5/5**, and the project itself came back
**0 findings**.

⚠️ **It must carry `--preview`, and omitting it fails in the worst-looking
way.** The rule is still preview-only; without the flag ruff emits a single
`warning: Selection PLW1514 has no effect because preview is not enabled` line
and then prints **`All checks passed!` and exits 0**. That is this project's own
recurring shape — an empty selection reads exactly like a clean result — except
here the tell is one quiet line above a green headline, which is worse than no
output at all. Whichever linter you reach for, run the positive control first
and confirm it actually fires.

The second thing stands unchanged, and it is why this is still not a pytest
gate: **neither linter is in `requirements.txt`**. Both are local developer
tools, so a gate means either adding a dependency or shipping a test that
silently skips on a fresh clone — and a permanently-skipped test is decoration.
Same ruling as the `EncodingWarning` audit: keep it as a documented command
line, run it when the encoding rule is touched.

    py -3 -m ruff check --isolated --preview --select PLW1514 axiomatic/*.py test/*.py *.py
    py -3 -m pylint --disable=all --enable=W1514 --score=n axiomatic/*.py test/*.py *.py

That makes four independent checks agreeing the encode/decode rule is clean —
this project's own static scan, the runtime `EncodingWarning` sweep, `W1514`,
and `PLW1514` — each with its own positive control. They are not redundant: the
static scan is narrow by design, the runtime sweep is the only one that sees
dependency code and variable argv, and the two linters are the only ones that
resolve a receiver they cannot see literally. Running both is not belt-and-
braces either: two implementations of one rule, each with its own clean result,
still have nothing comparing them until you put them on the same corpus.

**Those two were finally put on the same corpus, and they are not equals.**
A seven-shape control (`open`, `Path(...).open`,
`Path(...).read_text`, `Path(...).write_text`, and the same three arriving as a
`p: Path` **parameter**) scored **ruff 7/7, pylint 4/7**, with pylint's hits a
strict subset: it resolves a receiver spelled `Path(...)` *at the call site* but
misses every one that arrives as a parameter. That is the hard half — a receiver
written literally is also what this project's own narrow scan can see, so the
parameter form is the only shape a linter is here to add. `PLW1514` is the one
to reach for; keep the pylint line as an independent implementation (a hedge
against a ruff regression), not as extra coverage, and do not read a clean
pylint run as covering the indirect receiver. The project itself came back
**0 findings on both**.

**`encoding=` only names OUR end. A Python child you spawn has its own end,
and on a pipe it is NOT UTF-8.** When a child's stdout is a pipe rather than a
console, CPython encodes with the locale codec (cp950 here), so
`subprocess.run([sys.executable, …], encoding="utf-8")` decodes cp950 bytes as
UTF-8 — and `errors="replace"` turns that into a silent run of U+FFFD rather
than an exception. So spawning a Python child and reading its text output
means forcing the child too:
`env={**os.environ, "PYTHONIOENCODING": "utf-8"}`. Measured in a clean
environment: one 15-character Chinese line came back as **14** U+FFFD; a child
pytest run whose assertion message is Chinese came back with **60**, and 0 after
the fix. At the time only `_supervisor.stream_child` did this — the other **17**
sites set the decode end alone, and **three** of them carried a comment
correctly diagnosing the cp950 pipe before applying the fix to the wrong end.
`test_text_encoding.test_a_python_child_is_told_to_speak_utf8` now checks it
statically (three accepted shapes: a literal dict, an env built in the enclosing
function, a named helper).

**The one site that was already right was only half right.**
`stream_child` spelled it `env.setdefault("PYTHONIOENCODING", "utf-8")` while
its own docstring said the child is *forced* to UTF-8 — so a caller whose
environment already carried `PYTHONIOENCODING=cp950` won, in the single module
every launcher's log flows through, and the launcher's usual start paths
(desktop shortcut, autostart, scheduled task, somebody else's shell) are
exactly the ones that carry a value nobody set on purpose. It is an overwrite now,
like the other sites. The half worth noting is the **test** half: popping the
variable before measuring is the right thing to do (otherwise you measure the
developer's shell), but with the variable absent `setdefault` and an overwrite
behave identically, so that test alone can never see this. Measure both halves
— absent, and present-but-wrong.

**That static check reaches 16 of the 17, and the gap is structural, not an
oversight.** It decides "is this a Python child" by reading `call.args[0]`, so
it only sees an argv written **literally** at the call. Build the argv into a
variable first — `cmd = [sys.executable, "-u", str(SCRIPT)]; Popen(cmd, …)` —
and no scanner can tell what is being spawned. Two sites have that shape and
both need a **named** guard instead: `_supervisor.stream_child` (covered by
`test_supervisor.test_the_child_gets_utf8_io_encoding`) and
`verify_browser.run_full` (covered by
`test_verify_browser.test_the_full_verification_child_is_told_to_speak_utf8`).
The second one was **found only by asking which sites the scanner structurally
cannot see** — it had sat there through the whole 16-site sweep, in the entry
point this project mandates for browser verification, with a comment two lines
above correctly naming the cp950 pipe and then fixing the decode end. Measured
in a clean environment: three Traditional-Chinese progress lines came back as
**26** U+FFFD. Nobody noticed because the line that tool is *read* for —
`VERIFY-BROWSER: OK` — is pure ASCII and survives intact, so the tool looks
healthy while its diagnostics are destroyed. **A green static guard is not
evidence of full coverage; ask what shape it cannot parse.**

**Two traps live in this rule, and both cost a measurement here.** First, the
guard must compare **`ast.Constant` values for equality**, never a substring of
the unparsed source: the first version asked whether `"PYTHONIOENCODING"`
appeared anywhere in the enclosing function, which made it blind to the single
place that was already correct, because `stream_child`'s *docstring* explains
the variable — delete the code, keep the prose, stay green (mutation-tested:
SURVIVED, then KILLED after the fix). Second, **your own shell can hide this
entire class of bug**: the development shell here exports
`PYTHONIOENCODING=utf-8:surrogateescape`, so every child is accidentally
correct and "I ran it and it was fine" proves nothing. Strip `PYTHON*` before
measuring. `test_supervisor.test_the_child_gets_utf8_io_encoding` already did
exactly that — it pops the variable first, and says why in its docstring;
follow that pattern rather than inventing a new one. `stream_child` itself is
deliberately **not** covered by the static guard (its command line arrives as a
variable, so no scanner can tell it spawns Python); two behavioural tests are
its whole coverage — `test_the_child_gets_utf8_io_encoding` for the absent half
and `test_a_wrong_pythonioencoding_in_the_parent_is_overridden` for the
present-but-wrong half — and both are mutation-verified (4/4 killed; the
`setdefault` regression is killed by the second one alone).

**The rule has an encode half, and there it is a crash, not mojibake.** Everything
above is about *decoding*. Going the other way, `print()` on Windows takes one of
two completely different paths: to a **real console** CPython uses `WriteConsoleW`
and the code page never participates, but to a **pipe or file** — redirected,
captured by a tool, spawned by a launcher or a scheduled task — it uses the locale
codec, and a character cp950 cannot encode raises `UnicodeEncodeError`. **The
process dies**; it does not print badly. So anything this project *prints* must be
encodable in the OEM code page. Measured across 1433 print calls: three
strings were not (`⚠️`, `≈`), and **two of them sat on paths whose own comments said
the failure they report has no other symptom** — `⚠️` is the character you reach for
when writing a warning, and warnings are the lines that never run until something is
already wrong. Use `※` (U+203B) and `≒` (U+2252); `→ × ÷` are already fine. Three
layers hid this: a hand-run console is always fine, a dev shell that exports
`PYTHONIOENCODING` masks it, and those lines never execute. Guarded by
`test_text_encoding.test_nothing_printed_is_unencodable_on_this_console`, which scans
string **literals** inside `print` / `_progress` / `sys.std*.write` only — strings the
bot sends to the chat platform go out as UTF-8 and are deliberately out of scope.

**That guard reads the `print` call site, so it cannot see text assembled
somewhere else and printed by the caller.** The case that found this: `mutation_harness.Result.report()` held three unencodable characters
(`⛔`, `⚠️`×2), and it evades three separate predicates at once — the literals are
not in a `print(...)` argument, `mutation_harness.py` has no `__main__` so any
per-module "is this a CLI tool" test skips it, and **its printing callers are all
outside the repo**, because this project's own rule keeps one-off driver scripts
out of the tree. So "who prints this?" is a question the tree answers "nobody",
and that answer is indistinguishable from "nothing is wrong". Worth generalising:
**a rule kept for one reason can make another rule's dataflow analysis
structurally blind.** Covered now by a pair —
`test_text_built_for_stdout_is_encodable_even_when_its_printer_is_elsewhere`
(derived: non-bot functions returning joined text) plus the named
`_TEXT_FOR_STDOUT` registry, same shape as the `stream_child` / `run_full`
carve-out above. Two traps worth knowing: the two
checks masked each other (`report()` satisfies both, so deleting the named half
stayed green until a test isolated it), and with the tree clean the
violation-reporting code never ran on real data, so deleting `problems.append`
also stayed green until the detection moved into a helper with a synthetic
corpus.

## Git Commits

- **Commit messages must not reveal AI authorship.** No `Co-Authored-By`
  lines; no mention of "Claude", "Claude Code", "AI", "GPT", "Copilot", or
  other AI tool/model names anywhere in the message body, PR titles, PR
  descriptions, code comments, or documentation.

  **Both halves are enforced.** That sentence names two different corpora —
  commit messages, and *code comments or documentation* — and the guard that
  enforced it once read `git log` alone. `test_commit_authorship.py` now also
  scans every tracked `.py` (comments and string literals only, so an identifier
  like `codex` in `@bot ai codex` cannot cry wolf) and every tracked `.md`.
  Measured clean when added: 101 `.py` and 66 `.md`, zero hits outside the
  marker registry itself. This is the same shape DoD #3 was caught in — a rule
  written once, enforced per corpus — so the question to ask of any
  multi-corpus rule here is which corpus the guard actually reads. One trap before you touch the marker list: two markers are
  anchored to the start of a line, and a Python comment always reads
  `# Co-Authored-By: …`. The `#` is not whitespace, so the prose scan strips the
  comment marker first; without that step the rule's own named example is the
  one shape the scan cannot see.
- Commit subjects describe what changed, not who wrote it.
- **Stage files individually** (`git add path/to/file`), never `git add -A`.
  A wildcard add sweeps up whatever happens to be sitting in the tree — build
  artefacts, a stray screenshot, an editor's scratch file — and the one time it
  matters it will be something you cannot take back.

- **Credentials are never tracked.** `auth.md` and `discord_bot_token.md` are
  in `.gitignore`; the repo ships `auth.example.md` /
  `discord_bot_token.example.md` and the first-run step is to copy them. The
  same goes for `bot_config.json` (channel and owner ids, launch whitelist),
  the three `presence_*.json` files, and the queue / prompt files, which are the
  user's own content. **Do not move any of them back into the tracked set.**
  The reason is that the failure is irreversible: deleting the line afterwards
  does not help, the value is already in a commit, and the only real remedy is
  to rotate the credential.

  **A rule about *files* says nothing about *values*.** A secret can be pasted
  into a third place — a stack trace in a note, a fixture in a test, a README
  footer. None of those looks like a credential file and `git status` stays
  normal. So run a generic shape scanner when you touch anything
  credential-shaped. Not as a pytest gate, for the same reason as the
  `PLW1514` / `W1514` lines above: it is not in `requirements.txt`, so a gate
  would mean a new dependency or a test that silently skips on a fresh clone.

      gitleaks dir . --redact=100 --no-banner --report-format json --report-path <outside the repo>
      gitleaks git . --redact=100 --no-banner --report-format json --report-path <outside the repo>

  `--redact=100` is not optional — the report embeds the matched values by
  default, so an unredacted report is itself the next leak; write it outside the
  repo for the same reason.

  ⚠️ **Run the positive control before believing a clean result.** Measured on
  this project's own credential shapes, gitleaks scored **0/2**: fed a
  `username:` / `password:` pair and a bare 70-character token, it reported "no
  leaks found", because one is a plain `key: value` line and the other has no
  assignment syntax or nearby keyword. A shape scanner and a value scanner are
  blind in opposite directions and neither subsumes the other.
  **"gitleaks is clean" must never be read as "no credential is exposed."**
  Feed it a file you know is dirty first; if it does not fire, you have measured
  nothing.
- **A new repo-root runtime file MUST be classified the moment you add it** —
  project asset (tracked) or runtime artefact (gitignored, `.tmp` sibling
  included). `test_gitignore_coverage.py` extracts every
  `PROJECT_ROOT / "<literal>"` by AST and fails on anything unclassified, so
  this is fail-closed. Missing an entry is silent: the file is written, `git
  status` looks normal, and one slip of `git add` ships local data (that is
  how `.chrome_profile_verify/` — a copy of the login profile — once sat
  un-ignored).

  **The extractor only recognises a *bare* root name on the left**
  (`isinstance(node.left, ast.Name)`), so a chained path
  (`PROJECT_ROOT / "axiomatic" / "x.py"`) is invisible to it. That is
  deliberate and safe **only because of a premise**: where a chained path lands
  is decided by its leftmost segment, and that segment *is* extracted and
  classified. Measured: 12 chained sites, every first segment is
  `axiomatic` or `.venv`, both classified. The premise is now checked
  (`test_every_chained_root_path_is_anchored_by_a_classified_first_segment`),
  because the shape that breaks it has no first literal segment at all —
  `(PROJECT_ROOT).resolve() / "cache.tmp"` or `PROJECT_ROOT.parent / "x"` would
  land in the repo root with nothing looking at it. If that test goes red, widen
  the extractor; do not widen the test.

## Project structure

Layer map and entry points: `architecture.md`. Three standing rules:

- Production code goes into `axiomatic/`. The repo root holds only the
  launchers (`start_*.py`, `run_batch.py`, `install_autostart.py`,
  `wake_autostart.py`), the configuration and the docs.
- Tests live in `test/` at the repo root, **not** in the package. `pytest.ini`
  sets `testpaths = test` and `pythonpath = axiomatic .`, and `test/conftest.py`
  puts the same two directories on `sys.path`, so tests keep importing
  production modules by their top-level names (`import discord_bot`). A
  `test_*.py` or `conftest.py` dropped back into `axiomatic/` is **never
  collected** by a plain `py -3 -m pytest` — `test_docs_sync` fails on it.
  Scans that must see the tests (encoding, language, glob-shape, fingerprint,
  …) list `test/` explicitly next to the package.
- `docs/` (Sphinx), `README.md`, `COMMANDS.md` and `commands/` are the
  user-facing docs; `architecture.md` is the architecture overview (DoD #7).
  `CLAUDE.md` — this file — is the rulebook, and it is the **single source of
  record** for every rule a test reconciles against prose. Several guards parse
  this file literally (the atomic-write covered set, the permitted-channel
  paragraph, the `_pid_alive` enumeration, the owner-only group list, the
  Language word table): editing one of those sentences without editing its
  constant, or the reverse, goes red on purpose.
