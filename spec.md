# KOKORO // Audiobook Studio — Project Spec & Handoff

> **Purpose of this file:** complete state of the project so work can continue in a
> fresh session without any prior conversation context. Read top-to-bottom once;
> then jump to **§6 Status** and **§7 Remaining Work**.

---

## 1. Objective

Give the existing CLI tool `tts.py` (EPUB → chapter-wise audiobook WAVs via
Kokoro TTS) a **local web GUI** with:

- a button/zone to **add a `.epub` book** (drag & drop or click-to-browse)
- **progress bars** (overall + per-chapter paragraph progress + live log)
- chapter selection: **individual chapters, ranges** (plus ALL/NONE) — random N
  removed at user request 2026-08-26 (task **D1**)
- an option to **save audio to a user-chosen location** via a **▸ BROWSE**
  button next to the output field that opens a themed **in-page folder-picker
  modal** (breadcrumb / back / SELECT) which writes the chosen absolute path
  into the output field (task **D2**). A native OS folder dialog is not
  possible here — the output path is server-side, so the picker is in-page,
  fed by `GET /api/dirs`
- a **stop/abort** button (saves a partial WAV for the in-flight chapter)
- a **good, futuristic UI design** (dark, neon cyan/magenta, glassmorphism)
- local-only server (no cloud, no auth — binds to `127.0.0.1`)

The headless CLI in `tts.py` must keep working unchanged for terminal use.

---

## 2. Environment facts (verified 2025-08-26)

| Item | Value |
|---|---|
| Workspace | `/home/kunalc/SATA1TB/AI/dpHarness/tts-book-download` |
| Python | 3.12.13 from **conda env `archAI1`** (`/home/kunalc/.conda/envs/archAI1`) |
| Kokoro | 0.9.4, installed in conda env; **model cached** at `~/.cache/huggingface/hub/models--hexgrad--Kokoro-82M` |
| Other TTS deps | `soundfile`, `ebooklib`, `beautifulsoup4`, `numpy 1.26.4` — all in conda env |
| espeak-ng | present (`/usr/bin/espeak-ng`) — required by Kokoro |
| Sandbox constraint | this session's file sandbox is **workspace-write**: nothing outside the workspace (conda env, `~/.local`, pip caches) is writable. That is why deps were installed into a **workspace venv**, not the conda env. |
| GUI venv | `.venv/` (created with `--system-site-packages` so it reuses kokoro/torch/etc. from the conda env; contains `fastapi 0.141.1`, `uvicorn`, `python-multipart`) |
| Default port | **7860** (override: `--port`) |

### Recreate the venv if `.venv` is ever deleted:

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install fastapi "uvicorn[standard]" python-multipart
```

(If the venv is recreated **outside** this sandbox, `pip install` directly into
conda env `archAI1` also works — that's a normal terminal, not the sandbox.)

---

## 3. Files

| File | Status | What it is |
|---|---|---|
| `tts.py` | **modified** | Original CLI. Changes are backward-compatible (see §4.1 + §4.2) |
| `gui.py` | **new** | The whole GUI: FastAPI app + embedded single-page UI (no build step) |
| `spec.md` | **new** | This file |
| `make_test_epub.py` | **new** | Generates `test_book.epub` (4 short chapters) for testing |
| `test_book.epub` | generated | Tiny test book (4 chapters, ~187 words total) |
| `uploads/` | runtime dir | Where uploaded EPUBs are stored (`uploads/test_book.epub` exists) |
| `audiobook/` | runtime dir | Test-run output: `chapter_001.wav` … `chapter_004.wav` (24 kHz mono, ~20 s each) |
| `.venv/` | runtime | GUI virtualenv (do not delete; see §2) |

---

## 4. How it works (architecture)

### 4.1 `tts.py` changes (backward compatible)

1. **`import threading`** added at top.
2. **`get_chapters()`** — added a guard that skips non-document spine entries
   (`if not hasattr(item, "get_body_content"): continue`).
   *Bug fix:* the original code crashed on EPUBs whose spine references `ncx`/`nav`
   items (exposed by the test book). Real books with such spines would have hit it.
3. **`synthesise_chapter(...)`** gained two **optional** parameters (CLI callers
   unaffected):
   - `progress_cb: callable | None` — invoked from the worker thread with:
     - `{"type": "para", "para": n, "total_paras": t, "preview": str}` after each paragraph
     - `{"type": "chapter_done", "path": str, "duration_min": float}` on success
     - `{"type": "chapter_partial", "path": str, "duration_min": float}` on abort-with-partial
   - `stop_event: threading.Event | None` — checked at the start of **each paragraph**;
     if set, the existing partial-save path runs (writes `chapter_NNN_partial.wav`)
     and `StopRequested` is raised, exactly like Ctrl+C.

### 4.2 `gui.py` — FastAPI app

**One global `Job` object** (single concurrent job by design) protected by a
`threading.Lock`. A daemon worker thread runs `run_job()`:

```
init KPipeline → for each selected chapter (in order):
    mark active → synthesise_chapter(progress_cb, stop_event)
    → on StopRequested: mark rest skipped, status="stopped", exit
    → clean segment dir unless keep_segments (mirrors CLI)
status = "done" | "stopped" | "error"
```

**Endpoints**

| Method & path | Purpose |
|---|---|
| `GET /` | The UI (one embedded HTML string, `PAGE`) |
| `GET /api/meta` | Voices list, default voice, language codes |
| `GET /api/dirs` | Folder-picker (D2): `?path=...` optional (defaults to workspace, else `$HOME`) → `{dir, parent, exists, dirs[]}` — realpath-resolved, `dirs[]` directories only, sorted case-insensitive, well-formed on nonexistent/file paths (`exists:false`) |
| `POST /api/book` | multipart `file=@x.epub` → saves to `uploads/`, parses chapters, returns chapter list. Rejects (409) while a job runs |
| `POST /api/start` | JSON `{"chapters":[0,2,3], "voice":"am_adam", "lang":"a", "output":"./audiobook", "keep_segments":false}` → spawns worker thread |
| `POST /api/stop` | Sets the stop event (clean abort, partial WAV saved) |
| `GET /api/snapshot` | Full JSON state (status, chapters, selected, states[], log[], elapsed, error, output_abs) |
| `GET /api/stream` | **SSE**: full snapshot every 0.5 s — the browser's only live channel |

**Frontend (embedded in `gui.py`, vanilla JS, no dependencies)**

- **01 · SOURCE** — drop zone (drag/drop or click), book meta (name, chapter & word
  counts), chapter list with glowing checkboxes, and quick-select tools:
  `ALL` / `NONE` / `RANGE from–to` (these just manipulate the checkbox
  set; the final payload is always the explicit checked indices).
- **02 · SETTINGS** — voice dropdown (11 Kokoro voices), language `a`/`b`,
  **output location text field** (manual input still works) with a **▸ BROWSE**
  directory-picker button beside it that opens a themed in-page folder-picker
  modal (breadcrumb / back / SELECT THIS DIRECTORY; fed by `GET /api/dirs`)
  whose SELECT writes the absolute path into the output field, "keep TTS
  segments (debug)" toggle, and the big `INITIATE SYNTHESIS` button (becomes
  red `ABORT TRANSMISSION` while running).
- **03 · MISSION CONTROL** — overall % bar (done chapters + current chapter's
  paragraph fraction), current-chapter line, thin paragraph bar, and stat tiles:
  chapters done, elapsed time, total audio minutes.
- **04 · SYSTEM LOG** — terminal-style console (scanlines, colored levels:
  info/success/warn/error/para), auto-scroll, fed by the SSE `log[]` tail.
- Header **status pill** (IDLE / INITIALIZING / SYNTHESIZING / ABORTED /
  COMPLETE / FAULT) with pulsing dot; toasts for errors.
- On page load it fetches `/api/snapshot` to **restore state after a refresh**
  (a running job keeps showing live progress).
- Fonts: Orbitron / Rajdhani / Share Tech Mono via Google Fonts (graceful
  fallback to system fonts if offline).

---

## 5. How to run

```bash
cd /home/kunalc/SATA1TB/AI/dpHarness/tts-book-download

# Web GUI (default http://127.0.0.1:7860)
.venv/bin/python gui.py                 # add --port N / --host ADDR if needed

# Headless CLI (unchanged behaviour)
.venv/bin/python tts.py book.epub --list-chapters
.venv/bin/python tts.py book.epub --voice af_heart --chapters 1,3,5-10 --output ./audiobook
```

In-session note: in the session that built this, the server was left running as
a background bash job. In a **new session** that job is gone — just start it
again with the command above (kill whatever holds port 7860 first if needed).

### Reproduce the verified test (all of this already ran successfully)

```bash
.venv/bin/python make_test_epub.py                                    # → test_book.epub
.venv/bin/python gui.py &                                            # port 7860
curl -s -F "file=@test_book.epub" http://127.0.0.1:7860/api/book     # → 4 chapters
curl -s -X POST http://127.0.0.1:7860/api/start -H 'Content-Type: application/json' \
     -d '{"chapters":[0,1,2,3],"voice":"am_adam","lang":"a","output":"./audiobook","keep_segments":false}'
curl -s http://127.0.0.1:7860/api/snapshot                           # → status, states, log
# ~91 s later: status "done", 4 WAVs in ./audiobook/
```

---

## 6. Status — what is DONE

1. **Environment audit** — Python/conda/kokoro/model-cache/espeak-ng checked;
   workspace venv created with FastAPI stack. ✅
2. **`tts.py` GUI hooks** — `progress_cb` + `stop_event` on `synthesise_chapter`
   (optional params; CLI untouched). ✅
3. **`tts.py` robustness fix** — non-document spine entries (`ncx`/`nav`) no
   longer crash `get_chapters`. ✅
4. **`gui.py` backend** — FastAPI app: book upload/parse, job start/stop,
   snapshot, SSE stream, worker thread, error surfacing. ✅
5. **`gui.py` frontend** — full futuristic UI per §4.2 (all requested features:
   add-book button, progress bars, specific/range/random chapter selection,
   custom output location, stop button). ✅ (code complete; see §7 for what's unverified)
6. **End-to-end test run (verified via API):**
   - `GET /` serves the page; `GET /api/meta` returns voices
   - uploaded `test_book.epub` → 4 chapters parsed
   - started job for chapters 0–3 → completed in **91 s**, status `done`
   - `audiobook/chapter_001.wav` … `chapter_004.wav` exist, **24 kHz mono**, ~20 s each
   - progress states transitioned correctly (`pending → active(para n/n) → done`)
   - log captured per-paragraph + per-chapter + completion lines ✅
7. **Browser verification (user, 2026-08-26):** A1–A4 checked in a real browser
   — page renders & is interactive, live progress updates stream, abort
   mid-job works. A5 checked 2026-08-27 (user, real browser): page reloaded
   mid-job — state restored from `/api/snapshot`, job kept running.
   A6 verified 2026-08-27 (agent): non-epub → 400 JSON + toast (no stray file),
    start button disabled with no book / 0 selections, mid-job re-upload → 409 toast.

---

## 7. Remaining work / needs verification

Ordered by priority — **D (user-requested) is next**, then A5/A6. Items marked
**(verify)** are implemented but not yet exercised in a real browser;
**(new)** are not built yet.

### A. Verify in a real browser (6 of 6 done)
- [x] **A1 (verify)** Open `http://127.0.0.1:7860`, confirm the layout renders
      as intended (fonts, neon theme, grid, responsive column). Start a small job
      and watch: status pill, % bar, chapter row badges (PENDING/ACTIVE/DONE),
      paragraph bar, console scroll, stat tiles.
      verified 2026-08-26 (user, real browser): page renders and is interactive;
      small job ran with live pill, % bar, badges, paragraph bar, console, tiles.
- [x] **A2 (verify)** SSE live updates in the browser (the endpoint is a plain
      0.5 s loop; snapshot polling was verified, the SSE consumer path was not).
      verified 2026-08-26 (user, real browser): live progress streamed mid-job,
      so the SSE consumer path is exercised.
- [x] **A3 (verify)** **Abort path**: start a job, click ABORT mid-chapter.
      Expect: `chapter_NNN_partial.wav` written, remaining rows → SKIPPED,
      pill → ABORTED, status `stopped`.
      verified 2026-08-26 (user, real browser): abort works mid-job.
- [x] **A4 (verify)** Quick-select buttons ALL / NONE / RANGE / RANDOM in the UI.
      verified 2026-08-26 (user, real browser): exercised in UI.
      (Note: RANDOM N was removed per **D1** on 2026-08-26 — that removal
      supersedes the RANDOM part of this check.)
- [x] **A5 (verify)** Page refresh mid-job restores state from `/api/snapshot`.
      verified 2026-08-27 (user, real browser): ran a job, reloaded the page
      mid-job — state restored from `/api/snapshot` and the job continued.
- [x] **A6 (verify)** Error paths in UI: uploading a non-epub, starting with
      nothing selected (button should be disabled), uploading while a job runs (409 toast).
      verified 2026-08-27 (agent): non-epub upload → 400 JSON + toast with no stray
      uploads/ file; start button disabled with no book / 0 selections; mid-job
      re-upload → 409 + toast; no fixes needed.

### B. Known limitations (fix only if the user wants them)
- [ ] **B1 (new)** No **audio playback** of generated WAVs in the UI. If wanted:
      add `GET /audio/{path}` serving files from the job's output dir (resolve +
      verify the path stays inside the output dir before serving), and a play
      button per DONE chapter.
- [ ] **B2 (new)** No **resume** — a stopped/restarted job re-runs from the
      start. If wanted: persist last state (e.g. `audiobook/.job_state.json` with
      selected indices + done set) and offer "Resume" on load.
- [ ] **B3 (new)** Single-book, single-job by design. No queue of multiple books.
      *(B4 — "no directory browser, fine as-is" — was removed from this list on
      2026-08-26: the user now wants it built; it is tracked as **D2** below.)*
- [ ] **B5 (minor)** Voice preview not available (no way to hear a voice before
      committing to a whole book). Could synthesize a 2-sentence sample per voice
      into `sample_<voice>.wav` on demand.

### C. Hygiene
- [ ] **C1** Delete test artifacts if not wanted: `test_book.epub`,
      `make_test_epub.py`, `audiobook/`, `uploads/` (keep `.venv/` and `gui.py`).
- [ ] **C2** Optional: a short `README.md` with the run commands (§5) and a
      screenshot of the UI (take one after A1).
- [ ] **C3** Optional: guard against two `gui.py` instances on the same port
      (uvicorn already fails loudly; a friendly message could be added).

### D. User-requested changes (added 2026-08-26 — do next)
- [x] **D1 (new)** Remove the **RANDOM N** quick-select button (user no longer
      wants random chapter selection). Keep ALL / NONE / RANGE. Remove its JS
      handler along with the button. After the code change, update §1
      (chapter-selection bullet) and §4.2 (quick-select line) to drop RANDOM N.
      done 2026-08-26 (agent): gui.py — `RANDOM` button, `rN` input, `.mini.rand`
      CSS and the `#bRand` JS handler all removed (zero "rand" references left);
      §1 and §4.2 updated. Verified on a live server (port 7861): ALL/NONE/RANGE
      intact, page 200, `/api/snapshot` OK.
- [x] **D2 (done)** Add a **Select output directory** button next to the output
      text field (02 · SETTINGS) that opens a folder-picker window so the
      location is chosen by browsing, like the input file picker. *Design note
      (important):* a native OS folder dialog is **not** possible here — the
      browser's file dialog only picks client-side files, while the output
      path is server-side. Implement in-page instead: a server endpoint listing
      subdirectories (e.g. `GET /api/dirs?path=...` →
      `{dir, parent, exists, dirs[]}`, resolving/sanitizing the path, filtering
      to directories; start at the workspace or `$HOME`) + a modal with
      breadcrumb / back / select that writes the chosen path into the output
      field. Safe because the server is local-only and binds `127.0.0.1` (§2).
      Supersedes B4 ("fine as-is" no longer holds — the user wants it). After
      the code change, update §1 (save-audio bullet) and §4.2.
      done 2026-08-26 (agent): gui.py — `GET /api/dirs` endpoint (realpath-resolved,
      `dirs[]` directories only, sorted case-insensitive, never 500s on bad paths)
      + in-page themed folder-picker modal beside the output field (▸ BROWSE
      button; breadcrumb / back / SELECT THIS DIRECTORY) whose SELECT writes the
      absolute path into the output field; the text field still accepts manual
      input and `/api/start` is unchanged. Verified on a live server (port 7861):
      `GET /` 200 with modal markup, `/api/meta` OK, `/api/dirs` default =
      workspace with a directories-only listing, `?path=/tmp` → parent `/`,
      nonexistent path → `exists:false` with the server still healthy, file path
      (`spec.md`) → `exists:false`, `../` paths realpath-resolved; clean log (no
      tracebacks/warnings). §1 and §4.2 updated. Note: `audiobook/` contains
      test-run artifacts (chapter_001/002.wav, `_partial.wav` files,
      `chapter_003_segments/`) left by the verification runs.

---

## 8. Pitfalls for the next session

1. **Don't `pip install` into the conda env from inside the sandbox** — it's
   read-only here. Use `.venv` (workspace) or tell the user to do it in a normal
   terminal.
2. **Never delete `.venv/`** casually — recreating it is one command (§2), but
   forgetting it breaks `gui.py` with a confusing ImportError.
3. `gui.py` imports `tts.py` **and** `kokoro` at import time; the Kokoro model is
   already cached so job start is a few seconds, not a download.
4. The SSE endpoint streams **forever** (by design; the browser owns the
   connection). One open browser tab = one connection; fine for local use.
5. `Job` is a **global singleton** — don't add per-request jobs without
   reworking `states`/log ownership.
6. `synthesise_chapter`'s `progress_cb` runs **inside the worker thread**; the
   GUI's callback only mutates dicts under `job._lock` and appends logs — keep
   that pattern if you extend it (no re-entrant locking).
7. Chapter indices sent to `/api/start` are **0-based book indices** (not
   position-in-selection). `Job.states[pos-1]` maps selection position → state.
8. If the user's real books are large (1000+ chapters, 100k+ words), the first
   job will take hours; the stop button + partial saves are the safety net
   (verify with A3 on a real book).

## 9. Exact verified results (for reference)

- Test run: 4 chapters, ~187 words total, voice `am_adam`, lang `a`,
  output `./audiobook` → **91.1 s elapsed**, 4 × `chapter_00N.wav`
  (990 KB / 953 KB / 1044 KB / 875 KB; 20.6 / 19.9 / 21.8 / 18.2 s audio).
- `get_chapters('test_book.epub')` → 4 chapters, 40–55 words each, titles
  "Chapter One" … "Chapter Four".
- Voice list served: `af_heart, af_bella, af_nicole, af_sarah, af_sky,
  am_adam, am_michael, bf_emma, bf_isabella, bm_george, bm_lewis`
  (default `am_adam`).
