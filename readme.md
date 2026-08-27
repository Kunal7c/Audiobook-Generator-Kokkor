# Audiobook Studio — EPUB → WAV Audiobook Generator

Convert an EPUB book into chapter-wise WAV audio files using
[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) text-to-speech, with a
local web GUI and a headless CLI. Everything runs on your machine: the server
binds to `127.0.0.1` — no cloud, no account, no telemetry.

## Features

- **Web GUI** (one page, no build step):
  - drag & drop (or click-to-browse) an `.epub` book
  - live progress: overall %, per-chapter paragraph progress, terminal-style log
  - chapter selection: individual chapters, ranges, `ALL` / `NONE`
  - output location by manual input **or** an in-page folder picker
  - **Abort** mid-job — saves a partial WAV for the in-flight chapter
  - state survives page refreshes (a running job keeps streaming)
  - dark, futuristic theme (neon cyan/magenta, glassmorphism)
- **Headless CLI** — the same engine, scriptable
- **11 Kokoro voices**, American or British English
- **Local-only** — binds to `127.0.0.1`, no auth, no cloud

## Requirements

- Python 3.10+ (developed on 3.12)
- [espeak-ng](https://github.com/espeak-ng/espeak-ng) installed on the system
  (required by Kokoro)
- Python packages:
  - `kokoro-onnx` (imported as `kokoro`) — the [Kokoro-82M](https://github.com/thewh1teagle/Kokoro-82M)
    model is downloaded once on first run and cached in `~/.cache/huggingface`
  - `fastapi`, `uvicorn`, `python-multipart` — the web GUI
  - `ebooklib`, `beautifulsoup4`, `soundfile`, `numpy` — the TTS pipeline

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install fastapi "uvicorn[standard]" python-multipart
.venv/bin/pip install kokoro-onnx soundfile ebooklib beautifulsoup4 numpy
```

Install espeak-ng for your platform:

```bash
sudo apt install espeak-ng    # Debian / Ubuntu
brew install espeak-ng        # macOS
```

> First run downloads the Kokoro-82M model (~330 MB) and caches it — later
> starts are quick.

## Run the web GUI

```bash
.venv/bin/python gui.py                 # → http://127.0.0.1:7860
.venv/bin/python gui.py --port 8000     # custom port
.venv/bin/python gui.py --host 0.0.0.0  # custom bind address
```

Then open <http://127.0.0.1:7860> and:

1. **01 · Source** — drop a `.epub`; pick chapters (checkboxes,
   `ALL` / `NONE` / from–to range).
2. **02 · Settings** — voice, language (`a` American / `b` British English),
   output directory (type it, or use **▸ BROWSE** to pick a folder), and
   optionally "keep TTS segments" for debugging.
3. **INITIATE SYNTHESIS** — watch the overall/chapter progress and the log.
   **ABORT TRANSMISSION** stops the job and saves a partial WAV for the
   in-flight chapter.
4. WAVs appear in the chosen output directory (`./audiobook` by default):
   `chapter_001.wav`, `chapter_002.wav`, … — 24 kHz mono.

## Run the CLI

```bash
.venv/bin/python tts.py book.epub --list-chapters
.venv/bin/python tts.py book.epub --voice af_heart --lang a \
    --chapters 1,3,5-10 --output ./audiobook
.venv/bin/python tts.py --list-voices
```

Chapter selection accepts individual numbers, ranges, or a mix:
`5`, `1,3,5`, `5-10`, `1,3,5-8`, `last`.

## Files

| File | What it is |
|---|---|
| `gui.py` | Web GUI: FastAPI app + embedded single-page UI (no build step) |
| `tts.py` | TTS engine + headless CLI (EPUB → chapter WAVs) |
| `make_test_epub.py` | Generates a small 4-chapter test EPUB (`test_book.epub`) |
| `test_book.epub` | Tiny test book (generated) |
| `uploads/` | Where uploaded EPUBs are stored |
| `audiobook/` | Default output directory |

## Notes & limitations

- **One book, one job at a time** (by design) — no queue of multiple books.
- **No resume** — a stopped job re-runs from the start (the abort path saves a
  partial WAV instead).
- **No in-UI audio playback** — listen to the generated WAVs with any player.
- Large books (1000+ chapters) take hours; the abort button + partial saves
  are the safety net.
- The web server has no authentication; if you expose it beyond `127.0.0.1`
  (e.g. `--host 0.0.0.0`), put a tunnel or proxy in front of it.
