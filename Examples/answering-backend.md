# The answering backend

The bot has a pluggable answering backend ("Dorossi"). It can answer a one-off
question, or run an **autonomous loop** that keeps working across many rounds
until you stop it. All of `/dorossi` is owner-only.

## Ask a one-off question

A mention with free text is the question entry point. It stays a mention (not a
slash command) so one message can carry several lines, attachments and reply
context:

```
@bot <your question in plain language>
```

The backend replies in the same conversation and remembers context within a
session.

## Sessions and autonomous loops

Each conversation is a **session**. An autonomous loop keeps taking rounds on
its own:

```
/dorossi ask <task for an autonomous loop>
/dorossi running          # which rounds and loops are live right now
/dorossi abort            # stop the current loop
```

If the bot restarts, or a loop is interrupted by a network drop, resume every
interrupted loop at once:

```
/dorossi session continue all
```

`all` now resumes **loops you aborted yourself too** — earlier it skipped those
and you had to name each one. To keep a session out of `all` for good, archive
it:

```
/dorossi session delete <session id>
```

Automatic resume on reconnect/restart still only touches loops stopped by a
network drop or an interruption, never ones you aborted — `all` is the explicit,
owner-typed exception.

## Switch model or provider

Provider is a session setting; model can be set for one round or one session:

```
/dorossi ai <provider>            # switch the backend provider for this session
@bot /model <alias> <your question>   # switch the model for this session
```

The valid model aliases show up in the command menu, so you do not have to
memorise them. Each session is independent; a new session starts from the
default.

## A turn parked by the plan usage limit

If a round hits the plan's usage limit, the bot does **not** just drop it. The
turn **parks itself** in the queue with a wall-clock re-run time and comes back
on its own once the limit resets — you never have to remember to ask again.

While it is parked you can see and manage it:

```
/dorossi queue show       # each conversation shows "parked N until HH:MM"
/dorossi queue detail     # the id and exact re-run time per entry
/dorossi queue remove <id>   # cancel one parked (or waiting) entry by its id
/dorossi queue undo       # put a cancelled parked entry back to keep waiting
```

Re-parking does not count against the poison-queue retry cap (a stuck round is a
different problem from a used-up quota), and if the platform is unreachable at
the re-run moment the entry re-schedules itself rather than being dropped into
the failed queue.
