# Worked examples

Annotated, end-to-end walkthroughs of how the bot is operated day to day. These
expand on the short scenarios in the main [`README`](../README.md#worked-examples).

Every id, channel, name, prompt and tag below is a **placeholder** — replace the
`<...>` parts with your own. Nothing here is tied to a particular deployment.

Commands are written as **slash commands**, for a platform that has a native
slash menu. On a platform without one (see
[`../docs/platforms.md`](../docs/platforms.md)) the same commands are typed as
text instead; the walkthroughs note where that matters.

| Scenario | What it covers |
|---|---|
| [Run a batch](running-a-batch.md) | Filling the queues, the stop marker, starting, watching (`/gen current`, `/gen progress`, `/eta`), pausing and resuming |
| [The answering backend](answering-backend.md) | Asking a question, autonomous loops, `/dorossi session continue all`, switching model/provider, a usage-limit-parked turn |
| [Operating the host](operating-the-host.md) | `/proc usage` while a batch runs, running several platforms at once, which process supervises the batch |
| [Recovery](recovery.md) | A batch or loop surviving a network outage, a parked turn returning on its own — stated as generic capabilities |

## Ground rules these examples assume

- **Host-control and the answering backend are owner-only.** The whole `/proc`,
  `/host`, `/win`, `/input`, `/screen`, `/clip`, `/locate`, `/macro`, `/watch`,
  `/schedule` and `/launcher` groups, plus `/dorossi`, only answer the owner id
  configured per platform. Everyone else is refused before dispatch.
- **Replies are generic.** The bot never sends service names, host paths, file
  names, PIDs or raw exception text to non-owners; full detail goes to the log.
- **Queues are re-read live.** Editing a queue while a run is in flight takes
  effect at the next pair, not at the next run.
