# Recovery you do not have to babysit

The point of running unattended is that the ordinary interruptions — a network
blip, hitting a usage limit, a restart — resolve themselves. These are stated as
generic capabilities; you can reproduce them by simply causing the interruption.

## A batch surviving a network outage

If the batch loses network connectivity mid-run, it does not crash and it does
not silently skip work. It **stops in place**, tells the difference between "the
network is gone" and "something crashed", and resumes from the same point when
connectivity returns. There is no round limit on the wait; only `/stop` ends it.

You can watch this: `/status` shows the batch as waiting, and when the network
comes back the run continues at the pair it was on — because progress is a
resume checkpoint written atomically, not an in-memory position.

## An autonomous loop surviving the same outage

An autonomous answering loop behaves the same way. On a network drop it parks in
place and waits; on reconnect it picks up the same round. Nothing caps the wait
except an explicit `/dorossi abort`. After a bot restart:

```
/dorossi session continue all
```

resumes every interrupted loop (see [the answering backend](answering-backend.md)),
including ones you aborted yourself. Loops stopped by a network drop or a
restart are also picked up automatically when the bot reconnects.

## A turn parked by the plan usage limit

When a round hits the plan's usage limit, the turn **parks itself** in the queue
with a wall-clock re-run time and re-runs itself once the limit resets — you do
not have to remember to come back and ask again.

```
/dorossi queue show       # shows "parked N until HH:MM" per conversation
/dorossi queue detail     # the id and exact re-run time
/dorossi queue remove <id>   # cancel one parked entry, if you change your mind
```

The wait uses the machine-readable reset time when the limit gives one (with a
small buffer) and otherwise backs off; re-parking does not count against the
retry cap that protects against a genuinely stuck round, and an outage at the
re-run moment re-schedules the entry rather than dropping it.

## The common thread

All three cases share one design: the thing that must survive a kill lives on
disk (the batch checkpoint, the queue's parked entries), so a restart anywhere
is harmless and the work continues instead of being lost or silently abandoned.
