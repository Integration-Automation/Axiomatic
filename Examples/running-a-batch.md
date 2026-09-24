# Running a batch

Goal: queue up some work, start the browser batch, watch it, and pause/resume
without losing progress. All from chat.

## 1. Fill the queues

The batch pairs a **prompt** with **character** entries. You rarely hand-edit
the files — the `/todo` group writes them for you, one entry per line:

```
/todo prompt add    scenery, wide shot, soft light
/todo prompt add    interior, warm light
/todo char1 add     example-character
/todo char2 add     <second character, or leave the row empty for none>
/todo negp add      lowres, bad hands
```

Check what is queued and what will actually run:

```
/todo prompt list         # the raw prompt queue
/queue                    # remaining per queue + the pair count that will run
```

`/queue` is the honest number: it accounts for the pairing rules and the stop
marker, so it can be smaller than the raw line count.

## 2. Mark where the batch should stop

The **stop marker** (`end` sentinel) is where a run finishes even if more lines
follow. It lets you keep a backlog in the queue and only run part of it:

```
/todo prompt end          # insert the stop marker at the current tail
/todo prompt unend        # remove it again
```

`/gen plan` previews exactly which pairs will run up to the marker:

```
/gen plan
```

## 3. Start and watch

```
/run                      # start now
/run in 90m               # or schedule it
/run at 02:00             # or at a wall-clock time
```

While it runs:

```
/gen current              # the pair in flight and how many images so far
/gen progress             # done vs. remaining across the run
/eta                      # estimated finish (stops counting at the stop marker)
/status                   # batch state and which browser variant is running
```

Editing a queue now is safe — because queues are re-read for every character,
an entry you add takes effect at the **next pair**, not at the next run.

## 4. Pause, resume, stop

```
/gen pause
/gen resume               # the resume checkpoint is kept, so nothing re-runs twice
/stop                     # end the run
```

Progress is written atomically after each step, so even a hard kill resumes at
the right place: a pair only leaves the queue once enough images for it have
landed, so a crash mid-pair re-runs that pair rather than skipping it.

## Notes

- On a platform without a slash menu, the same actions are the text commands
  documented in [`../docs/platforms.md`](../docs/platforms.md).
- If every queue is empty the batch falls back to the `prompt.md` /
  `character1.md` / `undesired.md` files, so a first run needs only one queue
  filled.
