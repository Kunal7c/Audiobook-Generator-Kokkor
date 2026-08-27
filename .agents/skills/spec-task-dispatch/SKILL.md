---
name: spec-task-dispatch
description: Dispatch one task from spec.md to a fresh subagent session that gets its own full context window. Use when the user asks for a spec.md task (e.g. "test the abort button", "do A3", "pick the next task") or when the main session's context is running low and work must be offloaded.
---

# Spec task dispatch — one fresh subagent per spec.md task

The main chat is a **dispatcher**, not a worker. It never reads the whole
project into its own context. All heavy work happens in a fresh `subagent`
run, which starts with a completely empty context window (the full budget).
`spec.md` (in the project root) is the shared state: every subagent reads it
for context and writes its result back into it.

## When to use

- The user names a task: "test the abort button", "do A3", "verify SSE",
  "the next remaining task", "build the audio playback".
- The user asks to continue/verify/build something tracked in `spec.md` §7.

## Protocol

### 1. Pick the task (keep your own context small)

- Read `spec.md`. Focus on **§6 Status** and **§7 Remaining work** — those
  are the only sections needed to pick work. Do NOT read the project source
  files (`gui.py`, `tts.py`, …) in the main chat.
- Map the user's request to **one concrete task line** (e.g. `A3`). If it
  matches nothing in §7, confirm the task wording with the user first, then
  dispatch it as an ad-hoc task.
- **One task per subagent. Never bundle two tasks into one prompt.**

### 2. Spawn the subagent

Call `subagent` (background) with a **fully self-contained** prompt — the
subagent shares zero conversation context, so the prompt must stand alone.
Template:

> You are working in the project at `<absolute workspace path>`.
> First read `spec.md` in that directory top-to-bottom — it is the complete
> project state (environment §2, files §3, architecture §4, how to run §5,
> status §6, remaining work §7, pitfalls §8, verified results §9).
>
> **Your task: `<task id>` — `<task text, copied verbatim from spec.md §7>`**
>
> Do the task. Long-running processes (e.g. starting `gui.py` on port 7860)
> must be started with `run_in_background` and killed before you finish;
> kill any background server you started before returning.
>
> Then, before you finish:
> 1. Update `spec.md` with your result:
>    - in §7, change your task's `[ ]` to `[x]` and append a one-line
>      `verified YYYY-MM-DD: <what you checked + key evidence>` note under it;
>    - if you found a bug, fixed one, or hit a new limitation, add a short
>      line to the right §7 section (or §8 pitfalls);
>    - if you produced a new verified measurement, append it to §9.
> 2. Keep `spec.md` slim: short bullet lines only — no pasted logs, no code
>    dumps, no stack traces.
>
> Return a final summary of **at most 5 lines**: what you did, pass/fail,
> and anything the dispatcher must relay to the user.

Fill in the absolute workspace path (see spec.md §2) and the task text
**verbatim** from §7.

### 3. Track the run

- Note the subagent id and tell the user which task was dispatched.
- When the run settles you receive a notice containing the subagent's final
  message. Relay that summary (not the work) to the user.
- Follow-up about a task that already settled → dispatch a **fresh**
  subagent, referencing the earlier result.
- The subagent failed or stalled → dispatch a fresh subagent for the same
  task, adding one line: "previous attempt found `<X>`". Never try to resume
  a broken conversation.

### 4. Rules

1. **Never do the task yourself in the main chat** — not even "quick" ones.
   Your only work is: pick the task, write the prompt, relay the result.
2. **spec.md is the single source of truth.** If a subagent's summary
   contradicts spec.md, re-dispatch to verify — the file wins.
3. **Dispatch sequentially by default** (one subagent at a time). Multiple
   subagents all edit `spec.md`, and parallel edits can clobber each other.
   Only dispatch in parallel when tasks touch disjoint files, and keep each
   subagent's spec.md edit confined to its own task line.
4. **Keep spec.md small as it grows.** If §7 fills with done items, the user
   (or you, on request) may collapse them into short one-line entries — but
   never delete the §2/§4/§8 facts.
