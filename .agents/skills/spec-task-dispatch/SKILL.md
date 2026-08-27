---
name: spec-task-dispatch
description: Dispatch one task from spec.md to a fresh subagent session that gets its own full context window. Use when the user asks for a spec.md task (e.g. "test the abort button", "do A3", "pick the next task") or when the main session's context is running low and work must be offloaded.
---

# Spec task dispatch — one fresh subagent per spec.md task

The main chat is a **dispatcher**, not a worker. It never reads the whole
project into its own context. All heavy work happens in a fresh `subagent`
run, which starts with a completely empty context window (the full budget).
Two files hold the shared state:
- `techspec.md` (git-tracked) — environment, file map / loading recipe,
  **live contracts** (endpoint/field/element-id names), run/test commands,
  pitfalls, verified results.
- `spec.md` (local, gitignored) — tracker: goals, WIP status, open task
  lines, completed one-liners.

Every subagent reads `techspec.md` for context, loads only the files its
task touches, and writes its result back into `spec.md` (task line + one-liner)
and, when it verified something, `techspec.md` §7.

## When to use

- The user names a task: "test the abort button", "do A3", "verify SSE",
  "the next remaining task", "build the audio playback".
- The user asks to continue/verify/build something tracked in `spec.md` §7.

## Protocol

### 1. Pick the task (keep your own context small)

- Read `spec.md`. Focus on **§2 Status (WIP)** and **§3 Open tasks** — those
  are the only sections needed to pick work. Do NOT read the project source
  files (`studio/*.py`, `tts.py`, …) in the main chat.
- Map the user's request to **one concrete task line** (e.g. `T1a`). If it
  matches nothing in §3, confirm the task wording with the user first, then
  dispatch it as an ad-hoc task.
- **One task per subagent. Never bundle two tasks into one prompt.**
- A cross-file feature may be split into per-boundary subagents **only when
  the contract rows for both sides already exist in `techspec.md` §3** — the
  rows are the hand-off, so each side loads only its files.

### 2. Spawn the subagent

Call `subagent` (background) with a **fully self-contained** prompt — the
subagent shares zero conversation context, so the prompt must stand alone.
Template:

> You are working in the project at `<absolute workspace path>`.
> First read `techspec.md` in that directory — it is the technical reference
> and live contracts (environment §1, file map / loading recipe §2,
> contracts §3, run/test commands §4, pitfalls §6, verified results §7).
> Then load ONLY the project files your task touches (recipe in techspec.md
> §2; contract rows in §3 name exactly which files each side owns).
>
> **Your task: `<task id>` — `<task text, copied verbatim from spec.md §3>`**
>
> Do the task. Long-running processes (e.g. starting `gui.py` on port 7860)
> must be started with `run_in_background` and killed before you finish;
> kill any background server you started before returning.
>
> Then, before you finish:
> 1. Update the contract rows in `techspec.md` §3 if you added/renamed any
>    cross-file name (endpoint, field, element id, CSS class, function).
> 2. Update `spec.md` with your result:
>    - in §3, change your task's `[ ]` to `[x]` and append a one-line
>      `verified YYYY-MM-DD: <what you checked + key evidence>` note under it;
>    - if you produced a new verified measurement, append it to techspec.md §7.
> 3. Keep both files slim: short bullet lines only — no pasted logs, no code
>    dumps, no stack traces.
>
> Return a final summary of **at most 5 lines**: what you did, pass/fail,
> and anything the dispatcher must relay to the user.

Fill in the absolute workspace path (see techspec.md §1) and the task text
**verbatim** from `spec.md` §3.

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
2. **`techspec.md` + `spec.md` are the single source of truth.** If a
   subagent's summary contradicts them, re-dispatch to verify — the files
   win. For cross-file features the contract rows (techspec.md §3) are the
   hand-off: if code and a contract row disagree, re-dispatch to check.
3. **Dispatch sequentially by default** (one subagent at a time). Multiple
   subagents all edit `spec.md`, and parallel edits can clobber each other.
   Only dispatch in parallel when tasks touch disjoint `studio/` files **and**
   the contract rows for both sides already exist (techspec.md §3); keep each
   subagent's spec.md edit confined to its own task line.
4. **Keep the docs small as they grow.** If §3 of spec.md fills with done
   items, collapse them into short one-line entries in §4 — but never delete
   the techspec.md §1/§2/§6 facts.
