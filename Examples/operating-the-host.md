# Operating the host

The bot runs on a real machine. These are the owner-only commands for watching
and shaping what that machine is doing.

## See the machine's own load: `/proc usage`

Three reports answer different questions and deliberately do not overlap:

- `/gen current`, `/gen progress` — how far the **batch** has got.
- `/dorossi running` — which answering **rounds** are live.
- `/proc usage` — the **operating-system side**: how many processes this project
  is running, grouped by role, with memory and CPU per group, plus how much RAM
  and disk the whole machine has left.

```
/proc usage
```

A typical reading while a long batch runs shows the roles — the platform
process(es), the supervisor/launcher, the batch runner, the browser and driver,
the answering backend, and any other child processes — with the browser usually
the heaviest, and the whole-machine headroom at the bottom. That is exactly the
picture the other two reports cannot give you: they read progress and work
queues, this one reads the OS process table.

Notes:

- All browser processes are counted together as "browser and driver", using the
  same rule as the pre-run cleanup — so the count here matches what a cleanup
  would act on.
- A field the machine will not report shows `?` rather than `0`, and the last
  line says how many were missed — a zero that got summed into a total would be
  reported as fact.
- PIDs and the real output-disk path are shown to the owner only.

## Run several platforms at once

One supervised process per platform, each fully independent:

```
py -3 start_platforms.py          # start every enabled platform
py -3 start_platforms.py --list   # which will start, which will not, and why
py -3 start_discord_bot.py --platform <name>   # start just one
```

Each process keeps its own single-instance lock, its own log and its own state
under `state/<platform>/`, so restarting or switching one platform off never
touches the others. A platform with no credentials filled in is simply
**absent**, not failed — it is not started and does not complain on every boot.

To add a second platform: fill its `<platform>_bot_token.md`, set
`platforms.<platform>.enabled` to `true` in `bot_config.json` with that
platform's own `owner_user_ids` and `allowed_chat_ids`, and run
`start_platforms.py`.

## Which process supervises the batch

The batch is a **whole-machine** resource — one browser, one set of queue files,
one output folder — so it does not fork per platform. Whichever process holds
the batch lock supervises it; other processes that receive a batch-control
command reply that another process is already supervising, rather than opening a
second stack. Editing queues is unaffected: the queue files are on disk, so any
platform edits them equally.

## Autostart on login (Windows)

```
py -3 install_autostart.py --install    # idempotent
py -3 install_autostart.py --status
py -3 install_autostart.py --remove
```

This registers one task per enabled platform plus one for the batch. The trigger
is **logon**, not boot: the batch needs a real desktop session to open a browser.
