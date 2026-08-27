# techspec.md — Technical reference + live contracts

> **Rules for anyone editing (human or subagent):**
> - A name crossing a file boundary (endpoint, request/response field, element
>   id, CSS class, exported function) is a **contract** and lives in §3.
> - Adding/renaming a contract → **update the row in the same pass**. The
>   dispatcher checks code ⇄ contract agreement.
> - Keep this file lean. Facts belong in §1/§2/§6; contract rows in §3.
> - The tracker (goals / WIP / open tasks / completed) is `spec.md` (local,
>   gitignored). Subagents update `spec.md` §3–§4 for their task line.

## 1 · Environment
- Workspace: `/home/kunalc/SATA1TB/AI/dpHarness/tts-book-download`
- Python 3.12.13 (conda env `archAI1`). GUI venv `.venv/` built with
  `--system-site-packages` (fastapi 0.141.1, uvicorn 0.52.4, python-multipart).
  Rebuild: `python3 -m venv --system-site-packages .venv && .venv/bin/pip install fastapi "uvicorn[standard]" python-multipart`
- tts.py stack (site-packages): kokoro-onnx (as `kokoro`), soundfile, ebooklib,
  beautifulsoup4, numpy. Model cached in `~/.cache/huggingface` (~330 MB).
- **Run:** `.venv/bin/python gui.py` → `http://127.0.0.1:7860`
  (shim → `studio/main.py`; `--host` / `--port` supported).
- Runtime dirs (gitignored): `uploads/` (EPUBs), `audiobook/` (output WAVs),
  `preview_cache/` (voice-preview WAVs).
- espeak-ng required on the system (Kokoro).

## 2 · File map (per-task loading recipe)

| File | Responsibility | Load when |
|---|---|---|
| `studio/app.py` | FastAPI app instance, dir constants (`BASE_DIR`, `UPLOAD_DIR`, `UI_DIR`), `GET /`, `/static` mount | touching routing / static serving |
| `studio/state.py` | Job lifecycle: `Job`, `job`, `log()`, `snapshot()`, `make_cb()`, `run_job()` | touching job state / worker |
| `studio/api.py` | All `/api` endpoints (meta, dirs, book, start, stop, snapshot, stream, preview) | touching endpoints |
| `studio/main.py` | `main()`: argparse `--host/--port`, uvicorn entry | touching startup |
| `studio/ui/index.html` | HTML layout, element ids (contract §3.4) | touching UI layout/ids |
| `studio/ui/style.css` | Styles + classes | touching appearance |
| `studio/ui/app.js` | All JS behaviour (DOM refs at top: `const $ = …`) | touching behaviour |
| `tts.py` | Headless CLI + public API (§3.5) — **must keep working** | touching TTS |
| `gui.py` | Thin shim (~25 lines) → `studio.main.main()` | **never edit** |
| `make_test_epub.py` | Builds `test_book.epub` (4 ch, 40–55 words) | e2e fixtures |
| `spec.md` | Tracker (local) — update task lines §3 / one-liners §4 | always (subagents) |

- One UI feature = **one subagent owning that feature block across all three
  `ui/` files**.
- Subagents load only the files their task touches. The voice-preview
  backend⇄frontend split crosses **only the API contract** (§3.3).

## 3 · Contracts (live — update rows in the same pass)

### 3.1 API endpoints
| Method · Path | Purpose | Request | Response |
|---|---|---|---|
| `GET /api/meta` | voices / default / langs | — | `{"voices":[…11…],"default_voice":"am_adam","langs":{"a":"American English","b":"British English"}}` |
| `GET /api/dirs?q=` | folder browser | `?q=<abs path>` | dir listing JSON |
| `POST /api/book` | EPUB upload + chapter parse | multipart `file=@x.epub` | `{"name":str,"chapters":[{"index":i,"title":str,"words":int}]}` |
| `POST /api/start` | start the (single) job | JSON `StartReq` | `{"ok":true}` or 409 |
| `POST /api/stop` | stop running job | — | `{"ok":true}` or 409 |
| `GET /api/snapshot` | full job snapshot | — | JSON `Snapshot` (§3.2) |
| `GET /api/stream` | SSE snapshot every ~0.5 s | — | `data: {Snapshot}\n\n` |
| `GET /` | UI shell | — | `ui/index.html` |
| `GET /static/{file}` | `style.css`, `app.js` | — | file |

### 3.2 Data shapes
- **StartReq**: `chapters: list[int]` (0-based, required) · `voice: str = "am_adam"` · `lang: str = "a"` · `output: str = "./audiobook"` · `keep_segments: bool = false`
- **Snapshot**: `status` · `epub_name` · `chapters[{index,title,words}]` · `selected: list[int]` · `voice` · `lang` · `output` · `output_abs` · `keep_segments` · `states[]` · `log[{time,kind,text}]` · `error` · `elapsed`
- **states[]** (one per selected chapter, in order): `index` · `title` · `words` · `status` (`pending`→`active`→`done`/`partial`/`skipped`) · `para` · `paras` · `preview` · `path` · `duration_min`
- **Job.status** enum: `idle` | `initializing` | `running` | `stopped` | `done` | `error`
- **SSE frame**: `data: {Snapshot JSON}\n\n` every ~0.5 s, until client disconnects.
- **Voices** (11, default `am_adam`): `af_heart, af_bella, af_nicole, af_sarah, af_sky, am_adam, am_michael, bf_emma, bf_isabella, bm_george, bm_lewis`
- **Langs**: `a` = American English · `b` = British English
- **WAV**: 24 000 Hz mono 16-bit (browsers play natively).

### 3.3 Voice preview (build in progress — T1a/T1b)
- **Endpoint**: `POST /api/preview`
  - Request JSON: `{"text": str, "voice": str, "lang": str}`
  - Response: WAV file, `Content-Type: audio/wav`, `Content-Disposition: attachment; filename="preview_<hash8>.wav"`; errors: `400` (empty text / unknown voice).
  - Synthesises **any user text** (not chapters) via `tts.py`:
    `pipeline = KPipeline(lang_code=lang)` (import from `tts`), then
    `synthesise_chapter(pipeline, text, voice, out_dir, 0, verbose=False)`
    → WAV path. Cache `pipeline` per `lang` at module level in `api.py`
    (`_preview_pipeline: dict[str, KPipeline]`) so it is built once.
    Rename the produced WAV to `preview_<hash8>.wav` before serving.
  - **Stateless**: never touches `job`; a plain (sync) endpoint runs in
    FastAPI's thread pool + one module-level `threading.Lock` serialises
    preview synthesis; coexists with a running book job; 409-free by design.
  - **Cache**: same `(text, voice, lang)` → same WAV, stored in `preview_cache/`
    (gitignored, key = short hash); serve from cache on hit.
  - Frontend contract rows (ids, §3.4): block inside 02·Settings card:
    `#vp-toggle` (ADVANCED button), `#vp-panel` (hidden container),
    `#vp-text` (textarea), `#vp-generate` (GENERATE button),
    `#vp-audio` (`<audio controls>`), `#vp-status` (status line).
    JS in `app.js`: `vpGenerate()` — fetches `POST /api/preview`, sets
    `#vp-audio.src` to a blob URL, plays.
- Backend loads: `techspec.md` + `studio/api.py` (+ `tts.py` signatures §3.5 only
  if needed). Frontend loads: `techspec.md` + all three `studio/ui/` files.

### 3.4 UI element ids (index.html ⇄ app.js contract)
| id | element | notes |
|---|---|---|
| `dropzone` | 01·Source card | drag/drop EPUB |
| `file` | hidden input | EPUB file input |
| `bookmeta` | | chapter count / words line |
| `voice`, `lang` | selects | from `GET /api/meta` |
| `output` | text input | output dir |
| `keepseg` | checkbox | keep TTS segments |
| `chlist` | 03·Chapters list | per-chapter rows + `#selcount` |
| `startbtn` | INITIATE SYNTHESIS | `POST /api/start` |
| `pill`, `pilltxt`, `ppct`, `pfill`, `parafill` | 04·Progress | overall % |
| `curch` | | current chapter line |
| `console` | 04·Log | terminal-style log |
| `dirmodal` | folder picker modal | `GET /api/dirs` |
| `toast` | toast | transient messages |
| `vp-toggle` … `vp-status` | voice-preview block | §3.3 (build in progress) |

### 3.5 tts.py public API (must keep working)
- `AVAILABLE_VOICES: list[str]` (11 voices, §3.2)
- `StopRequested` (exception — stop requested)
- `get_chapters(epub_path: str) -> list[dict]` — `{"index":i,"title":str,"word_count":int,"text":str,…}` (keys used: `index,title,word_count,text`)
- `synthesise_chapter(pipeline, text, voice, chapter_dir, chapter_index, verbose=True, progress_cb=None, stop_event=None) -> Path`
  - `progress_cb(dict)`: `{"type":"para","para":n,"total_paras":t,"preview":str}` and `{"type":"chapter_done","path":str,"duration_min":float}`
- CLI: `.venv/bin/python tts.py book.epub --voice am_adam --lang a --chapters 1,3 --output ./audiobook` · `--list-voices` · `--list-chapters`

## 4 · Run / test commands
- GUI: `.venv/bin/python gui.py` (port 7860). CLI: see §3.5.
- Test book: `make_test_epub.py` → `test_book.epub` (4 ch, 40–55 words).
- **e2e curl recipe** (server must be up):
  1. `curl -s localhost:7860/api/meta`
  2. `curl -s -F "file=@test_book.epub" localhost:7860/api/book`
  3. `curl -s -X POST localhost:7860/api/start -H 'content-type: application/json' -d '{"chapters":[0,1,2,3],"voice":"am_adam","lang":"a","output":"./audiobook","keep_segments":false}'`
  4. Poll `curl -s localhost:7860/api/snapshot` → `status:"done"`; 4 WAVs in `audiobook/`.
  5. Abort: start again, `curl -s -X POST localhost:7860/api/stop` → `status:"stopped"`, in-flight chapter saved as partial.
  6. Refresh-restore: `GET /api/snapshot` after reload shows last state.
- Subagents: start servers with `run_in_background`, **kill before returning**.

## 5 · Subagent conventions
- Fresh session, self-contained prompt, ≤5-line return summary.
- Load only `techspec.md` + files in scope (recipe §2/§3.3).
- Update contract rows + `spec.md` task line in the same pass.
- Keep `techspec.md` / `spec.md` slim: short bullets, no logs, no code dumps.

## 6 · Pitfalls
- `studio/app.py` defines `BASE_DIR` = **project root** (`Path(__file__).parent.parent`); `UPLOAD_DIR` and `UI_DIR` derive from it. Don't re-create the app in other modules.
- One book, one job: `POST /api/start` while a job runs → 409.
- SSE `stream` polls `snapshot()` every 0.5 s and keeps the client alive; disconnect-safe (check `request` scope / catch per iteration).
- KPipeline init is slow (~tens of s) — it runs inside the worker thread; the `Job.status` goes `initializing`→`running` around it.
- WAVs are 24 kHz mono — don't resample.
- `gui.py` must stay a shim; logic goes in `studio/`.
- Kokoro model lives in `~/.cache/huggingface` — a fresh machine needs the download once.

## 7 · Verified results
- 4-chapter test book: first GUI run 91.1 s (cold), 4 WAVs (990/953/1044/875 KB; 20.6/19.9/21.8/18.2 s).
- Post-refactor e2e (studio/ package): 4 ch done in 10.4 s (warm), 4 WAVs; abort path verified (`stopped`, partial + skipped); `/`, `/static/style.css`, `/static/app.js`, `/api/meta` all OK (2026-08-27).
