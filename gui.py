#!/usr/bin/env python3
"""
Kokoro Audiobook Studio — web GUI for tts.py
============================================

A futuristic local web UI to convert EPUB books into chapter-wise
audiobook WAVs using Kokoro TTS, with live progress, chapter
selection (manual / range), abort support and a custom
output location.

Run:
    .venv/bin/python gui.py                # → http://127.0.0.1:7860
    .venv/bin/python gui.py --port 9000

Dependencies (already in .venv):
    fastapi, uvicorn, python-multipart
    (plus the tts.py stack: kokoro, soundfile, ebooklib, beautifulsoup4, numpy)

The CLI in tts.py is untouched — use it for headless runs.
"""

import argparse
import json
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from tts import (
    AVAILABLE_VOICES,
    StopRequested,
    get_chapters,
    synthesise_chapter,
)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"

app = FastAPI(title="Kokoro Audiobook Studio")


# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------

class Job:
    """Mutable state of the (single) synthesis job."""

    def __init__(self):
        self._lock = threading.Lock()
        self.status = "idle"          # idle | initializing | running | stopped | done | error
        self.epub_name = ""
        self.epub_path: Path | None = None
        self.chapters: list[dict] = []
        self.selected: list[int] = []
        self.voice = "am_adam"
        self.lang = "a"
        self.output = "./audiobook"
        self.keep_segments = False
        self.states: list[dict] = []  # one entry per selected chapter
        self.log: list[dict] = []
        self.error: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._stop = threading.Event()


job = Job()


def log(level: str, msg: str):
    line = {"level": level, "msg": msg, "t": time.time()}
    with job._lock:
        job.log.append(line)
        if len(job.log) > 600:
            job.log = job.log[-600:]


def snapshot() -> dict:
    with job._lock:
        elapsed = None
        if job.started_at is not None:
            end = job.finished_at if job.finished_at else time.time()
            elapsed = round(end - job.started_at, 1)
        return {
            "status": job.status,
            "epub_name": job.epub_name,
            "chapters": [
                {"index": i, "title": c["title"], "words": c["word_count"]}
                for i, c in enumerate(job.chapters)
            ],
            "selected": list(job.selected),
            "voice": job.voice,
            "lang": job.lang,
            "output": job.output,
            "output_abs": str(Path(job.output).expanduser().resolve()) if job.output else "",
            "keep_segments": job.keep_segments,
            "states": [dict(s) for s in job.states],
            "log": [dict(x) for x in job.log],
            "error": job.error,
            "elapsed": elapsed,
        }


def make_cb(st: dict):
    """Build a progress callback bound to one chapter's state dict."""

    def cb(ev: dict):
        with job._lock:
            if ev["type"] == "para":
                st["para"] = ev["para"]
                st["paras"] = ev["total_paras"]
                st["preview"] = ev["preview"]
            elif ev["type"] == "chapter_done":
                st["status"] = "done"
                st["path"] = ev["path"]
                st["duration_min"] = ev["duration_min"]
            elif ev["type"] == "chapter_partial":
                st["status"] = "partial"
                st["path"] = ev["path"]
                st["duration_min"] = ev["duration_min"]
        if ev["type"] == "para":
            log("para", f"Para {ev['para']}/{ev['total_paras']}: {ev['preview']}…")
        elif ev["type"] == "chapter_done":
            log("success", f"Chapter {st['index'] + 1} complete → {Path(ev['path']).name}  ({ev['duration_min']:.1f} min)")
        elif ev["type"] == "chapter_partial":
            log("warn", f"Partial chapter {st['index'] + 1} saved → {Path(ev['path']).name}  ({ev['duration_min']:.1f} min)")

    return cb


def run_job():
    """Worker thread: synthesise all selected chapters in order."""
    try:
        log("info", f"Initialising Kokoro pipeline (lang={job.lang}, voice={job.voice})…")
        from kokoro import KPipeline

        pipeline = KPipeline(lang_code=job.lang)
        with job._lock:
            job.status = "running"

        out = Path(job.output).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        with job._lock:
            job.output = str(out.resolve())
            n_total = len(job.selected)
            selected = list(job.selected)
        log("info", f"Synthesising {n_total} chapter(s) → {out.resolve()}")
        job.started_at = time.time()

        for pos, idx in enumerate(selected, start=1):
            if job._stop.is_set():
                break
            ch = job.chapters[idx]
            st = job.states[pos - 1]
            with job._lock:
                st["status"] = "active"
            log("info", f"Chapter {idx + 1}  [{pos}/{n_total}]  {ch['title']}  ({ch['word_count']:,} words)")

            chapter_dir = out / f"chapter_{idx + 1:03d}_segments"
            try:
                synthesise_chapter(
                    pipeline=pipeline,
                    text=ch["text"],
                    voice=job.voice,
                    chapter_dir=chapter_dir,
                    chapter_index=idx,
                    verbose=False,
                    progress_cb=make_cb(st),
                    stop_event=job._stop,
                )
            except StopRequested:
                with job._lock:
                    if st["status"] != "partial":
                        st["status"] = "skipped"
                    for s2 in job.states[pos:]:
                        if s2["status"] in ("pending", "active"):
                            s2["status"] = "skipped"
                    job.status = "stopped"
                    job.finished_at = time.time()
                log("warn", "Synthesis aborted by user.")
                return

            # Clean up segment files unless --keep-segments (mirrors CLI)
            if not job.keep_segments and chapter_dir.exists():
                for seg in chapter_dir.glob("seg_*.wav"):
                    try:
                        seg.unlink()
                    except OSError:
                        pass
                try:
                    chapter_dir.rmdir()
                except OSError:
                    pass

        with job._lock:
            job.status = "stopped" if job._stop.is_set() else "done"
            job.finished_at = time.time()
        for s2 in job.states:
            with job._lock:
                if s2["status"] in ("pending", "active"):
                    s2["status"] = "skipped"
        if job._stop.is_set():
            log("warn", "Synthesis aborted by user.")
        else:
            log("success", f"All selected chapters complete. Audio in {out.resolve()}")

    except Exception as e:  # noqa: BLE001 — surface any error to the UI
        with job._lock:
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            job.finished_at = time.time()
        log("error", job.error)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class StartReq(BaseModel):
    chapters: list[int]
    voice: str = "am_adam"
    lang: str = "a"
    output: str = "./audiobook"
    keep_segments: bool = False


@app.get("/api/meta")
def api_meta():
    return {
        "voices": AVAILABLE_VOICES,
        "default_voice": "am_adam",
        "langs": {"a": "American English", "b": "British English"},
    }


@app.get("/api/dirs")
def api_dirs(path: str = ""):
    """Directory browser for the in-page folder picker (read-only, no worker).

    `path` optional; empty → workspace dir (fall back to $HOME). Relative
    paths resolve against the workspace dir; `~`, `..` and symlinks are
    resolved deterministically. Never raises on bad input (nonexistent
    paths, files, odd strings) — returns a well-formed payload with
    `exists: false` and empty `dirs[]` instead.
    """
    home = Path.home()

    def start_dir() -> Path:
        return BASE_DIR if BASE_DIR.is_dir() else home

    try:
        if not path:
            d = start_dir()
        else:
            p = Path(path).expanduser()
            d = p if p.is_absolute() else start_dir() / p
        d = d.resolve()
    except (OSError, RuntimeError, ValueError):
        d = start_dir()

    exists = d.is_dir()
    parent = d.parent if d != d.parent else None
    entries = []
    if exists:
        try:
            for e in d.iterdir():
                try:
                    if e.is_dir():
                        entries.append({"name": e.name, "path": str(e)})
                except OSError:
                    pass
        except OSError:
            entries = []
    entries.sort(key=lambda x: (x["name"].lower(), x["name"]))
    return {
        "dir": str(d),
        "parent": str(parent) if parent else None,
        "exists": exists,
        "dirs": entries,
    }


@app.post("/api/book")
def api_book(file: UploadFile):
    """Upload + parse an EPUB. Replaces the currently loaded book."""
    name = Path(file.filename or "book.epub").name
    if not name.lower().endswith(".epub"):
        raise HTTPException(400, "Only .epub files are supported.")
    with job._lock:
        if job.status in ("initializing", "running"):
            raise HTTPException(409, "Stop the current job before loading another book.")

    data = file.file.read()
    if len(data) > 500 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 500 MB).")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / name
    dest.write_bytes(data)
    log("info", f"Book received: {name}  ({len(data) / 1e6:.1f} MB)")

    try:
        chapters = get_chapters(str(dest))
    except Exception as e:  # noqa: BLE001
        log("error", f"Failed to parse EPUB: {type(e).__name__}: {e}")
        raise HTTPException(500, f"Could not parse EPUB: {e}")
    if not chapters:
        raise HTTPException(400, "No readable chapters found in this EPUB.")

    with job._lock:
        job.epub_name = name
        job.epub_path = dest
        job.chapters = chapters
        job.selected = []
        job.states = []
    log("success", f"{len(chapters)} chapters found in {name}.")
    return {
        "name": name,
        "chapters": [
            {"index": i, "title": c["title"], "words": c["word_count"]}
            for i, c in enumerate(chapters)
        ],
    }


@app.post("/api/start")
def api_start(req: StartReq):
    with job._lock:
        if job.status in ("initializing", "running"):
            raise HTTPException(409, "A synthesis job is already in progress.")
        if not job.chapters:
            raise HTTPException(400, "Upload a book first.")
        total = len(job.chapters)
        selected = sorted({i for i in req.chapters if 0 <= i < total})
        if not selected:
            raise HTTPException(400, "Select at least one chapter.")
        if req.voice not in AVAILABLE_VOICES:
            raise HTTPException(400, f"Unknown voice: {req.voice}")
        if req.lang not in ("a", "b"):
            raise HTTPException(400, "Language must be 'a' or 'b'.")

        job.selected = selected
        job.voice = req.voice
        job.lang = req.lang
        job.output = req.output or "./audiobook"
        job.keep_segments = req.keep_segments
        job.states = [
            {
                "index": i,
                "title": job.chapters[i]["title"],
                "words": job.chapters[i]["word_count"],
                "status": "pending",
                "para": 0,
                "paras": 0,
                "preview": "",
                "path": None,
                "duration_min": None,
            }
            for i in selected
        ]
        job.error = None
        job.started_at = None
        job.finished_at = None
        job._stop = threading.Event()
        job.status = "initializing"

    log("info", f"Job queued: {len(selected)} chapter(s), voice={req.voice}, lang={req.lang}, out={job.output}")
    threading.Thread(target=run_job, daemon=True).start()
    return {"ok": True}


@app.post("/api/stop")
def api_stop():
    with job._lock:
        if job.status not in ("initializing", "running"):
            raise HTTPException(409, "No job is running.")
        job._stop.set()
    log("warn", "Stop requested — finishing current paragraph…")
    return {"ok": True}


@app.get("/api/snapshot")
def api_snapshot():
    return snapshot()


@app.get("/api/stream")
def api_stream():
    """Server-Sent Events: full snapshot every ~0.5 s."""

    def gen():
        while True:
            yield f"data: {json.dumps(snapshot())}\n\n"
            time.sleep(0.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/", include_in_schema=False)
def index():
    return HTMLResponse(PAGE)


# ---------------------------------------------------------------------------
# Page (embedded, single-file UI)
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KOKORO // Audiobook Studio</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Rajdhani:wght@400;500;600;700&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
  :root {
    --bg0: #04060d;
    --card: rgba(10, 16, 32, 0.72);
    --line: rgba(0, 229, 255, 0.16);
    --line-strong: rgba(0, 229, 255, 0.45);
    --cyan: #00e5ff;
    --mag: #c84dff;
    --green: #00ffa3;
    --amber: #ffc857;
    --red: #ff4d6d;
    --text: #d7e4f7;
    --dim: #6b7c99;
    --mono: "Share Tech Mono", ui-monospace, monospace;
    --disp: "Orbitron", "Rajdhani", sans-serif;
    --body: "Rajdhani", "Segoe UI", sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    background: var(--bg0);
    color: var(--text);
    font-family: var(--body);
    font-size: 15px;
    overflow-x: hidden;
  }
  body::before {
    content: ""; position: fixed; inset: 0; z-index: -2;
    background:
      radial-gradient(1100px 620px at 88% -10%, rgba(200, 77, 255, 0.16), transparent 62%),
      radial-gradient(1000px 700px at -12% 112%, rgba(0, 229, 255, 0.13), transparent 62%),
      var(--bg0);
  }
  body::after {
    content: ""; position: fixed; inset: 0; z-index: -1; pointer-events: none;
    background-image:
      linear-gradient(rgba(0, 229, 255, 0.045) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0, 229, 255, 0.045) 1px, transparent 1px);
    background-size: 44px 44px;
    -webkit-mask-image: radial-gradient(ellipse at center, black 30%, transparent 92%);
    mask-image: radial-gradient(ellipse at center, black 30%, transparent 92%);
  }

  /* ---------- header ---------- */
  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 18px 26px 10px;
  }
  .logo {
    font-family: var(--disp); font-weight: 900; font-size: 20px;
    letter-spacing: 0.22em; color: #eaf6ff;
    text-shadow: 0 0 18px rgba(0, 229, 255, 0.45);
  }
  .logo .thin { color: var(--cyan); font-weight: 500; }
  .logo .sub { color: var(--dim); font-weight: 500; font-size: 12px; letter-spacing: 0.34em; }

  .pill {
    display: flex; align-items: center; gap: 9px;
    font-family: var(--mono); font-size: 12px; letter-spacing: 0.22em;
    padding: 7px 16px; border: 1px solid; border-radius: 4px;
    background: rgba(5, 10, 22, 0.6);
  }
  .pill .dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: currentColor; box-shadow: 0 0 10px currentColor;
    animation: pulse 1.6s ease-in-out infinite;
  }
  .pill.dim   { color: var(--dim);   border-color: rgba(107,124,153,.4); }
  .pill.amber { color: var(--amber); border-color: rgba(255,200,87,.45); }
  .pill.cyan  { color: var(--cyan);  border-color: rgba(0,229,255,.5); }
  .pill.green { color: var(--green); border-color: rgba(0,255,163,.45); }
  .pill.red   { color: var(--red);   border-color: rgba(255,77,109,.5); }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }

  /* ---------- layout ---------- */
  main {
    display: grid; grid-template-columns: minmax(380px, 44%) 1fr;
    gap: 18px; padding: 8px 26px 26px; align-items: start;
  }
  @media (max-width: 980px) { main { grid-template-columns: 1fr; } }
  .col { display: flex; flex-direction: column; gap: 18px; min-width: 0; }

  .card {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 18px 18px 20px;
    backdrop-filter: blur(14px);
    box-shadow: 0 10px 40px rgba(0, 0, 0, 0.45), inset 0 1px 0 rgba(255,255,255,0.04);
  }
  .card-h {
    font-family: var(--disp); font-size: 11px; font-weight: 700;
    letter-spacing: 0.3em; color: var(--cyan);
    display: flex; align-items: center; gap: 10px; margin-bottom: 16px;
  }
  .card-h::after {
    content: ""; flex: 1; height: 1px;
    background: linear-gradient(90deg, var(--line-strong), transparent);
  }

  /* ---------- dropzone ---------- */
  #dropzone {
    border: 1px dashed rgba(0, 229, 255, 0.35);
    border-radius: 10px;
    padding: 26px 16px;
    text-align: center;
    cursor: pointer;
    transition: all .25s;
    background: rgba(0, 229, 255, 0.025);
  }
  #dropzone:hover, #dropzone.drag {
    border-color: var(--cyan);
    background: rgba(0, 229, 255, 0.07);
    box-shadow: 0 0 30px rgba(0, 229, 255, 0.15) inset;
  }
  #dropzone.locked { opacity: .45; pointer-events: none; }
  .dz-icon {
    font-size: 30px; color: var(--cyan);
    text-shadow: 0 0 16px rgba(0,229,255,.8);
    animation: float 2.6s ease-in-out infinite;
  }
  @keyframes float { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-6px); } }
  .dz-title {
    font-family: var(--disp); font-size: 13px; letter-spacing: 0.28em;
    margin-top: 10px; color: #eaf6ff;
  }
  .dz-sub { font-size: 13px; color: var(--dim); margin-top: 4px; letter-spacing: .08em; }

  .bookmeta { margin-top: 14px; }
  .bm-name {
    font-family: var(--mono); font-size: 14px; color: var(--cyan);
    word-break: break-all;
  }
  .bm-stats { font-family: var(--mono); font-size: 11px; color: var(--dim); letter-spacing: .14em; margin-top: 5px; }
  .hidden { display: none; }

  /* ---------- chapter selection ---------- */
  .selhead {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    margin: 16px 0 8px; flex-wrap: wrap;
  }
  .selhead > span:first-child {
    font-family: var(--disp); font-size: 10px; letter-spacing: 0.3em; color: var(--dim);
  }
  .seltools { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .mini {
    font-family: var(--mono); font-size: 10px; letter-spacing: .14em;
    color: var(--cyan); background: rgba(0,229,255,.06);
    border: 1px solid rgba(0,229,255,.3); border-radius: 4px;
    padding: 4px 9px; cursor: pointer; transition: all .15s;
  }
  .mini:hover { background: rgba(0,229,255,.16); box-shadow: 0 0 12px rgba(0,229,255,.25); }
  .mini-in {
    width: 52px; font-family: var(--mono); font-size: 11px; color: var(--text);
    background: rgba(255,255,255,.04); border: 1px solid var(--line);
    border-radius: 4px; padding: 4px 6px; text-align: center;
  }
  .mini-in:focus { outline: none; border-color: var(--line-strong); }
  input[type=number]::-webkit-outer-spin-button,
  input[type=number]::-webkit-inner-spin-button { -webkit-appearance: none; }
  .dash { color: var(--dim); }
  .sep { width: 1px; height: 16px; background: var(--line); margin: 0 4px; }
  #selcount { font-family: var(--mono); font-size: 11px; color: var(--green); letter-spacing: .1em; margin-left: 6px; }

  #chlist {
    max-height: 330px; overflow-y: auto;
    border: 1px solid rgba(0,229,255,.1); border-radius: 8px;
    background: rgba(2, 5, 12, 0.5);
    padding: 6px;
  }
  #chlist::-webkit-scrollbar { width: 8px; }
  #chlist::-webkit-scrollbar-track { background: rgba(255,255,255,.02); }
  #chlist::-webkit-scrollbar-thumb { background: rgba(0,229,255,.25); border-radius: 4px; }
  .empty { color: var(--dim); font-family: var(--mono); font-size: 12px; letter-spacing: .2em; text-align: center; padding: 26px 0; }

  .ch {
    display: grid; grid-template-columns: 38px 1fr auto auto 18px;
    align-items: center; gap: 10px;
    padding: 7px 10px; border-radius: 6px;
    border-left: 2px solid transparent;
    cursor: pointer; user-select: none;
    transition: background .15s;
  }
  .ch + .ch { margin-top: 2px; }
  .ch:hover { background: rgba(0,229,255,.05); }
  .ch.checked { background: rgba(0,229,255,.07); border-left-color: var(--cyan); }
  .ch .num { font-family: var(--mono); font-size: 11px; color: var(--dim); }
  .ch.checked .num { color: var(--cyan); }
  .ch .ttl { font-size: 14px; font-weight: 600; letter-spacing: .02em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ch .words { font-family: var(--mono); font-size: 10px; color: var(--dim); letter-spacing: .08em; }
  .ch .badge {
    font-family: var(--mono); font-size: 9px; letter-spacing: .12em;
    padding: 2px 7px; border-radius: 3px; min-width: 58px; text-align: center;
    color: var(--dim); border: 1px solid rgba(107,124,153,.3);
  }
  .ch .cb { width: 14px; height: 14px; border: 1px solid var(--line-strong); border-radius: 3px; position: relative; }
  .ch.checked .cb { background: var(--cyan); box-shadow: 0 0 8px rgba(0,229,255,.7); }
  .ch.checked .cb::after {
    content: ""; position: absolute; inset: 4px 3px;
    border: solid #021018; border-width: 0 0 2px 0; transform: rotate(-45deg) translate(1px, -1px);
  }
  .ch.active { border-left-color: var(--mag); }
  .ch.active .badge { color: var(--mag); border-color: rgba(200,77,255,.5); animation: pulse 1.4s infinite; }
  .ch.done .badge { color: var(--green); border-color: rgba(0,255,163,.5); }
  .ch.partial .badge { color: var(--amber); border-color: rgba(255,200,87,.5); }
  .ch.skipped .badge { color: var(--dim); }

  /* ---------- settings ---------- */
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media (max-width: 560px) { .grid2 { grid-template-columns: 1fr; } }
  .fld { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
  .fld > span, .fld span.lbl {
    font-family: var(--disp); font-size: 9px; letter-spacing: .28em; color: var(--dim);
  }
  select, .text {
    font-family: var(--mono); font-size: 13px; color: var(--text);
    background: rgba(255,255,255,.04); border: 1px solid var(--line);
    border-radius: 6px; padding: 9px 10px; width: 100%;
  }
  select:focus, .text:focus { outline: none; border-color: var(--line-strong); box-shadow: 0 0 14px rgba(0,229,255,.12); }
  select option { background: #0a1020; }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 4px 0 16px; flex-wrap: wrap; }
  .dimtxt { font-family: var(--mono); font-size: 10px; color: var(--dim); letter-spacing: .06em; max-width: 60%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .switch { display: flex; align-items: center; gap: 10px; cursor: pointer; font-family: var(--mono); font-size: 11px; letter-spacing: .12em; color: var(--text); }
  .switch input { display: none; }
  .switch .track {
    width: 34px; height: 18px; border-radius: 10px;
    background: rgba(255,255,255,.08); border: 1px solid var(--line);
    position: relative; transition: all .2s;
  }
  .switch .track::after {
    content: ""; position: absolute; top: 2px; left: 2px;
    width: 12px; height: 12px; border-radius: 50%;
    background: var(--dim); transition: all .2s;
  }
  .switch input:checked + .track { background: rgba(0,255,163,.15); border-color: rgba(0,255,163,.5); }
  .switch input:checked + .track::after { left: 18px; background: var(--green); box-shadow: 0 0 8px rgba(0,255,163,.8); }

  .big {
    width: 100%; padding: 15px;
    font-family: var(--disp); font-size: 14px; font-weight: 700; letter-spacing: 0.3em;
    color: #021018; cursor: pointer;
    background: linear-gradient(90deg, var(--cyan), #6ea8ff 55%, var(--mag));
    border: none; border-radius: 8px;
    filter: drop-shadow(0 0 18px rgba(0, 229, 255, 0.35));
    clip-path: polygon(14px 0, 100% 0, 100% calc(100% - 14px), calc(100% - 14px) 100%, 0 100%, 0 14px);
    transition: filter .2s, transform .1s;
  }
  .big:hover:not(:disabled) { filter: drop-shadow(0 0 28px rgba(0, 229, 255, 0.6)); }
  .big:active:not(:disabled) { transform: translateY(1px); }
  .big:disabled { filter: grayscale(.7) brightness(.6); cursor: not-allowed; }
  .big.danger {
    background: linear-gradient(90deg, var(--red), #ff8a5c);
    filter: drop-shadow(0 0 20px rgba(255, 77, 109, 0.5));
  }

  /* ---------- progress ---------- */
  .prog-top { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 10px; flex-wrap: wrap; }
  .prog-pct {
    font-family: var(--disp); font-size: 34px; font-weight: 900; color: #eaf6ff;
    text-shadow: 0 0 22px rgba(0, 229, 255, 0.55);
  }
  .prog-cur { font-family: var(--mono); font-size: 11px; color: var(--cyan); letter-spacing: .1em; max-width: 55%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: right; }

  .bar {
    position: relative; height: 16px; border-radius: 4px; overflow: hidden;
    background: rgba(255,255,255,.045); border: 1px solid var(--line);
  }
  .bar > i {
    position: absolute; inset: 0; width: 0%;
    background: linear-gradient(90deg, var(--cyan), var(--mag));
    box-shadow: 0 0 14px rgba(0, 229, 255, 0.6);
    transition: width .45s ease;
  }
  .bar > i::after {
    content: ""; position: absolute; inset: 0;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,.35), transparent);
    animation: shimmer 1.8s linear infinite;
  }
  @keyframes shimmer { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }
  .bar.thin { height: 7px; margin-top: 6px; }

  .subbar-lab { font-family: var(--disp); font-size: 9px; letter-spacing: .28em; color: var(--dim); margin-top: 16px; }

  .statgrid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 18px; }
  .stat {
    border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px;
    background: rgba(255,255,255,.02); text-align: center;
  }
  .stat b { display: block; font-family: var(--mono); font-size: 17px; color: var(--cyan); letter-spacing: .05em; }
  .stat span { font-family: var(--disp); font-size: 8px; letter-spacing: .24em; color: var(--dim); }

  /* ---------- console ---------- */
  .console {
    background: #02040a;
    border: 1px solid rgba(0,229,255,.14); border-radius: 8px;
    font-family: var(--mono); font-size: 11.5px; line-height: 1.75;
    padding: 12px 14px; height: 300px; overflow-y: auto;
    position: relative;
  }
  .console::-webkit-scrollbar { width: 8px; }
  .console::-webkit-scrollbar-track { background: rgba(255,255,255,.02); }
  .console::-webkit-scrollbar-thumb { background: rgba(0,229,255,.25); border-radius: 4px; }
  .console::before {
    content: ""; position: absolute; inset: 0; pointer-events: none;
    background: repeating-linear-gradient(0deg, rgba(0, 255, 163, 0.025) 0 1px, transparent 1px 3px);
  }
  .line { white-space: pre-wrap; word-break: break-word; }
  .line.info { color: #7fd4ff; }
  .line.success { color: var(--green); }
  .line.warn { color: var(--amber); }
  .line.error { color: var(--red); }
  .line.para { color: #46587a; }

  /* ---------- toast ---------- */
  #toast {
    position: fixed; bottom: 26px; left: 50%; transform: translateX(-50%) translateY(20px);
    font-family: var(--mono); font-size: 12px; letter-spacing: .08em;
    color: var(--cyan); background: rgba(4, 10, 22, 0.92);
    border: 1px solid rgba(0,229,255,.4); border-radius: 6px;
    padding: 10px 20px; opacity: 0; pointer-events: none; transition: all .3s; z-index: 50;
    box-shadow: 0 0 24px rgba(0,229,255,.2);
  }
  #toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
  #toast.err { color: var(--red); border-color: rgba(255,77,109,.5); box-shadow: 0 0 24px rgba(255,77,109,.25); }

  /* ---------- folder picker (D2) ---------- */
  .outrow { display: flex; align-items: center; gap: 8px; }
  .outrow .text { flex: 1; min-width: 0; width: auto; }
  .outrow .mini { white-space: nowrap; padding: 7px 12px; }

  .modal {
    position: fixed; inset: 0; z-index: 40;
    background: rgba(2, 5, 12, 0.72);
    backdrop-filter: blur(6px);
    display: flex; align-items: center; justify-content: center;
  }
  .modal.hidden { display: none; }
  .modal-box {
    width: min(640px, calc(100vw - 36px));
    background: var(--card);
    border: 1px solid var(--line-strong);
    border-radius: 12px;
    backdrop-filter: blur(14px);
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6), 0 0 42px rgba(0, 229, 255, 0.12), inset 0 1px 0 rgba(255,255,255,.04);
    padding: 16px 18px 18px;
  }
  .modal-h {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    font-family: var(--disp); font-size: 11px; font-weight: 700; letter-spacing: 0.3em; color: var(--cyan);
    margin-bottom: 12px;
  }
  .dm-crumbs {
    font-family: var(--mono); font-size: 11px; letter-spacing: .05em; color: var(--dim);
    border: 1px solid var(--line); border-radius: 6px;
    background: rgba(2, 5, 12, 0.5);
    padding: 7px 10px; margin-bottom: 10px;
    word-break: break-all;
  }
  .dm-crumbs .crumb { color: var(--cyan); cursor: pointer; }
  .dm-crumbs .crumb:hover { text-shadow: 0 0 8px rgba(0, 229, 255, .8); }
  .dm-crumbs .crumb.here { color: var(--text); cursor: default; }
  .dm-crumbs .crumb.here.missing { color: var(--amber); }
  .dm-crumbs .csep { color: var(--dim); margin: 0 3px; }
  .dm-list {
    max-height: 330px; overflow-y: auto;
    border: 1px solid rgba(0,229,255,.1); border-radius: 8px;
    background: rgba(2, 5, 12, 0.5);
    padding: 6px;
  }
  .dm-list::-webkit-scrollbar { width: 8px; }
  .dm-list::-webkit-scrollbar-track { background: rgba(255,255,255,.02); }
  .dm-list::-webkit-scrollbar-thumb { background: rgba(0,229,255,.25); border-radius: 4px; }
  .dm-row {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 10px; border-radius: 6px;
    border-left: 2px solid transparent;
    font-family: var(--mono); font-size: 12.5px;
    cursor: pointer; user-select: none; transition: background .15s;
  }
  .dm-row + .dm-row { margin-top: 2px; }
  .dm-row:hover { background: rgba(0,229,255,.07); border-left-color: var(--cyan); }
  .dm-row .ico { color: var(--cyan); }
  .dm-row.up .ico { color: var(--amber); }
  .dm-row .nm { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dm-f {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    margin-top: 14px; flex-wrap: wrap;
  }
  .big.small { width: auto; padding: 10px 18px; font-size: 11px; letter-spacing: .22em; }
</style>
</head>
<body>

<header>
  <div class="logo">◢ KOKORO<span class="thin"> //</span>AUDIOBOOK <span class="sub">STUDIO</span></div>
  <div id="pill" class="pill dim"><span class="dot"></span><span id="pilltxt">IDLE</span></div>
</header>

<main>
  <!-- ============ left column ============ -->
  <section class="col">
    <div class="card">
      <div class="card-h">01 · SOURCE</div>
      <div id="dropzone">
        <input type="file" id="file" accept=".epub" hidden>
        <div class="dz-icon">▼</div>
        <div class="dz-title" id="dzTitle">DROP .EPUB FILE</div>
        <div class="dz-sub" id="dzSub">or click to browse</div>
      </div>
      <div id="bookmeta" class="bookmeta hidden">
        <div class="bm-name" id="bookName">—</div>
        <div class="bm-stats"><span id="bmCh">0</span> CHAPTERS &nbsp;·&nbsp; <span id="bmWords">0</span> WORDS</div>
      </div>

      <div class="selhead">
        <span>CHAPTER SELECT</span>
        <span class="seltools">
          <button class="mini" id="bAll">ALL</button>
          <button class="mini" id="bNone">NONE</button>
          <span class="sep"></span>
          <input class="mini-in" id="rA" type="number" min="1" placeholder="from">
          <span class="dash">–</span>
          <input class="mini-in" id="rB" type="number" min="1" placeholder="to">
          <button class="mini" id="bRange">RANGE</button>
          <b id="selcount">0/0</b>
        </span>
      </div>
      <div id="chlist"><div class="empty">NO BOOK LOADED</div></div>
    </div>

    <div class="card">
      <div class="card-h">02 · SETTINGS</div>
      <div class="grid2">
        <label class="fld"><span>VOICE</span><select id="voice"></select></label>
        <label class="fld"><span>LANGUAGE</span>
          <select id="lang">
            <option value="a">a — American English</option>
            <option value="b">b — British English</option>
          </select>
        </label>
      </div>
      <label class="fld"><span>OUTPUT LOCATION</span>
        <div class="outrow">
          <input id="output" class="text" type="text" value="./audiobook" spellcheck="false">
          <button type="button" class="mini" id="browseBtn" title="Browse the server filesystem">▸ BROWSE</button>
        </div>
      </label>
      <div class="row">
        <label class="switch">
          <input type="checkbox" id="keepseg">
          <span class="track"></span>
          KEEP TTS SEGMENTS (DEBUG)
        </label>
        <span id="outAbs" class="dimtxt"></span>
      </div>
      <button id="startbtn" class="big" disabled>INITIATE SYNTHESIS</button>
    </div>
  </section>

  <!-- ============ right column ============ -->
  <section class="col">
    <div class="card">
      <div class="card-h">03 · MISSION CONTROL</div>
      <div class="prog-top">
        <div class="prog-pct" id="ppct">0.0%</div>
        <div class="prog-cur" id="curch">—</div>
      </div>
      <div class="bar"><i id="pfill"></i></div>
      <div class="subbar-lab">CURRENT CHAPTER</div>
      <div class="bar thin"><i id="parafill"></i></div>
      <div class="statgrid">
        <div class="stat"><b id="stCh">0/0</b><span>CHAPTERS DONE</span></div>
        <div class="stat"><b id="stTime">00:00</b><span>ELAPSED</span></div>
        <div class="stat"><b id="stAudio">—</b><span>AUDIO MADE</span></div>
      </div>
    </div>

    <div class="card">
      <div class="card-h">04 · SYSTEM LOG</div>
      <div id="console" class="console"></div>
    </div>
  </section>
</main>

<!-- ============ folder picker modal (D2) ============ -->
<div id="dirmodal" class="modal hidden">
  <div class="modal-box">
    <div class="modal-h">
      <span>SELECT OUTPUT DIRECTORY</span>
      <button type="button" class="mini" id="dmClose" title="Close">✕</button>
    </div>
    <div id="dmCrumbs" class="dm-crumbs"></div>
    <div id="dmList" class="dm-list"></div>
    <div class="dm-f">
      <button type="button" class="big small" id="dmSelect">SELECT THIS DIRECTORY</button>
      <button type="button" class="mini" id="dmCancel">CANCEL</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
"use strict";
const $ = (s) => document.querySelector(s);

const state = {
  book: null,        // {name, chapters:[{index,title,words}]}
  snap: null,        // latest server snapshot
  renderedLog: 0,
  es: null,
  rowEls: {},
};
const selection = new Set();

/* ---------- helpers ---------- */
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}
function fmtT(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ":" : "") + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}
let toastTimer;
function toast(msg, isErr) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ""; }, 3400);
}
function busy() {
  return state.snap && (state.snap.status === "running" || state.snap.status === "initializing");
}

/* ---------- rendering ---------- */
const PILL = {
  idle: ["IDLE", "dim"], initializing: ["INITIALIZING", "amber"],
  running: ["SYNTHESIZING", "cyan"], stopped: ["ABORTED", "amber"],
  done: ["COMPLETE", "green"], error: ["FAULT", "red"],
};
function renderStatus() {
  const s = state.snap;
  if (!s) return;
  const [txt, cls] = PILL[s.status] || PILL.idle;
  $("#pill").className = "pill " + cls;
  $("#pilltxt").textContent = txt;
  const btn = $("#startbtn");
  const b = busy();
  btn.textContent = b ? "ABORT TRANSMISSION" : "INITIATE SYNTHESIS";
  btn.disabled = b ? false : (!state.book || selection.size === 0);
  btn.classList.toggle("danger", b);
  $("#dropzone").classList.toggle("locked", b);
}

function renderChapters() {
  const wrap = $("#chlist");
  wrap.innerHTML = "";
  state.rowEls = {};
  if (!state.book) {
    wrap.innerHTML = '<div class="empty">NO BOOK LOADED</div>';
    updateSelCount();
    return;
  }
  for (const c of state.book.chapters) {
    const row = document.createElement("div");
    row.className = "ch" + (selection.has(c.index) ? " checked" : "");
    row.dataset.idx = c.index;
    row.innerHTML =
      '<span class="num">' + String(c.index + 1).padStart(3, "0") + "</span>" +
      '<span class="ttl" title="' + esc(c.title) + '">' + esc(c.title) + "</span>" +
      '<span class="words">' + c.words.toLocaleString() + "w</span>" +
      '<span class="badge">—</span>' +
      '<span class="cb"></span>';
    row.addEventListener("click", () => {
      if (busy()) return;
      if (selection.has(c.index)) selection.delete(c.index);
      else selection.add(c.index);
      row.classList.toggle("checked", selection.has(c.index));
      updateSelCount();
    });
    wrap.appendChild(row);
    state.rowEls[c.index] = row;
  }
  updateSelCount();
  applyStates();
}

const BADGE = {
  pending: "PENDING", active: "ACTIVE", done: "DONE",
  partial: "PARTIAL", skipped: "SKIPPED",
};
function applyStates() {
  const s = state.snap;
  if (!s) return;
  // reset badges
  for (const idx in state.rowEls) {
    const row = state.rowEls[idx];
    row.className = row.className.replace(/\b(active|done|partial|skipped)\b/g, "").trim();
    if (selection.has(Number(idx))) row.classList.add("checked");
    row.querySelector(".badge").textContent = "—";
  }
  for (const x of s.states) {
    const row = state.rowEls[x.index];
    if (!row) continue;
    row.classList.add(x.status);
    row.querySelector(".badge").textContent = BADGE[x.status] || x.status.toUpperCase();
    if (x.status === "active" && x.paras) {
      row.querySelector(".badge").textContent = "P " + x.para + "/" + x.paras;
    }
  }
}

function computeProgress() {
  const s = state.snap;
  const st = s ? s.states : [];
  if (!st.length) return { pct: 0, cur: null };
  let done = 0;
  let cur = null;
  for (const x of st) {
    if (x.status === "done" || x.status === "partial") done++;
    if (x.status === "active") cur = x;
  }
  let pct = (done / st.length) * 100;
  if (cur && cur.paras) pct += (cur.para / cur.paras) * (100 / st.length);
  return { pct: Math.min(100, pct), cur };
}

function renderProgress() {
  const s = state.snap;
  if (!s) return;
  const { pct, cur } = computeProgress();
  $("#pfill").style.width = pct.toFixed(1) + "%";
  $("#ppct").textContent = pct.toFixed(1) + "%";
  $("#curch").textContent = cur
    ? "CH " + String(cur.index + 1).padStart(3, "0") + " · " + cur.title
    : s.status === "done" ? "ALL CHAPTERS COMPLETE"
    : s.status === "stopped" ? "TRANSMISSION ABORTED"
    : s.status === "error" ? "FAULT"
    : "STANDBY";
  $("#parafill").style.width = (cur && cur.paras) ? (cur.para / cur.paras * 100) + "%" : "0%";
  let done = 0;
  for (const x of s.states) if (x.status === "done" || x.status === "partial") done++;
  $("#stCh").textContent = done + "/" + s.states.length;
  $("#stTime").textContent = s.elapsed ? fmtT(s.elapsed) : "00:00";
  const totalMin = s.states.reduce((a, x) => a + (x.duration_min || 0), 0);
  $("#stAudio").textContent = totalMin ? totalMin.toFixed(1) + " MIN" : "—";
  $("#outAbs").textContent = s.output_abs ? "→ " + s.output_abs : "";
}

function renderConsole() {
  const s = state.snap;
  if (!s) return;
  const logArr = s.log;
  const box = $("#console");
  if (logArr.length < state.renderedLog) { box.innerHTML = ""; state.renderedLog = 0; }
  while (state.renderedLog < logArr.length) {
    const e = logArr[state.renderedLog++];
    const d = document.createElement("div");
    d.className = "line " + (e.level || "info");
    d.textContent = "[" + new Date(e.t * 1000).toTimeString().slice(0, 8) + "] " + e.msg;
    box.appendChild(d);
  }
  box.scrollTop = box.scrollHeight;
}

function renderBookMeta() {
  if (!state.book) { $("#bookmeta").classList.add("hidden"); return; }
  $("#bookmeta").classList.remove("hidden");
  $("#bookName").textContent = state.book.name;
  $("#bmCh").textContent = state.book.chapters.length;
  $("#bmWords").textContent = state.book.chapters.reduce((a, c) => a + c.words, 0).toLocaleString();
}

function updateSelCount() {
  const total = state.book ? state.book.chapters.length : 0;
  $("#selcount").textContent = selection.size + "/" + total;
  renderStatus();
}

function onSnap(s) {
  state.snap = s;
  renderStatus();
  renderProgress();
  applyStates();
  renderConsole();
}

/* ---------- actions ---------- */
function setSel(set) {
  selection.clear();
  for (const i of set) selection.add(i);
  renderChapters();
}
function allIdx() { return new Set(state.book ? state.book.chapters.map((c) => c.index) : []); }

async function upload(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".epub")) { toast("Only .epub files are supported", true); return; }
  const fd = new FormData();
  fd.append("file", file);
  $("#dzTitle").textContent = "TRANSMITTING…";
  $("#dzSub").textContent = file.name;
  try {
    const r = await fetch("/api/book", { method: "POST", body: fd });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { toast(j.detail || "Upload failed", true); resetDz(); return; }
    state.book = j;
    selection.clear();
    j.chapters.forEach((c) => selection.add(c.index));
    $("#dzTitle").textContent = "BOOK LOADED";
    renderBookMeta();
    renderChapters();
    toast(j.chapters.length + " chapters found");
  } catch (e) {
    toast("Upload error: " + e, true);
    resetDz();
  }
}
function resetDz() {
  $("#dzTitle").textContent = "DROP .EPUB FILE";
  $("#dzSub").textContent = "or click to browse";
}

async function start() {
  const body = {
    chapters: [...selection].sort((a, b) => a - b),
    voice: $("#voice").value,
    lang: $("#lang").value,
    output: $("#output").value.trim() || "./audiobook",
    keep_segments: $("#keepseg").checked,
  };
  const r = await fetch("/api/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) toast(j.detail || "Start failed", true);
}

async function stop() {
  const r = await fetch("/api/stop", { method: "POST" });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) toast(j.detail || "Stop failed", true);
}

/* ---------- folder picker (02 · SETTINGS) ---------- */
const dm = { dir: "", parent: null, exists: false };

function dmRow(name, path, isUp) {
  const row = document.createElement("div");
  row.className = "dm-row" + (isUp ? " up" : "");
  row.title = path;
  row.innerHTML = '<span class="ico">' + (isUp ? "↑" : "▸") + '</span><span class="nm"></span>';
  row.querySelector(".nm").textContent = name;
  row.addEventListener("click", () => dmLoad(path));
  return row;
}

function renderCrumbs() {
  const el = $("#dmCrumbs");
  el.innerHTML = "";
  const parts = dm.dir.split("/").filter(Boolean);
  const segs = [{ label: "/", path: "/" }];
  let acc = "";
  for (const p of parts) { acc += "/" + p; segs.push({ label: p, path: acc }); }
  segs.forEach((s, i) => {
    const here = i === segs.length - 1;
    const c = document.createElement("span");
    c.className = "crumb" + (here ? " here" : "") + (here && !dm.exists ? " missing" : "");
    c.textContent = s.label;
    c.title = s.path;
    if (!here) c.addEventListener("click", () => dmLoad(s.path));
    el.appendChild(c);
    if (!here) {
      const sep = document.createElement("span");
      sep.className = "csep";
      sep.textContent = " / ";
      el.appendChild(sep);
    }
  });
}

function renderDirList(j) {
  const el = $("#dmList");
  el.innerHTML = "";
  if (dm.parent) el.appendChild(dmRow("..", dm.parent, true));
  if (!j.exists) {
    el.insertAdjacentHTML("beforeend", '<div class="empty">PATH DOES NOT EXIST YET — SELECT WILL CREATE IT</div>');
  } else if (!j.dirs.length) {
    el.insertAdjacentHTML("beforeend", '<div class="empty">NO SUBDIRECTORIES</div>');
  } else {
    for (const d of j.dirs) el.appendChild(dmRow(d.name, d.path, false));
  }
}

async function dmLoad(path) {
  const q = path ? "?path=" + encodeURIComponent(path) : "";
  let j;
  try {
    const r = await fetch("/api/dirs" + q);
    j = await r.json().catch(() => ({}));
    if (!r.ok || typeof j.dir !== "string") throw new Error((j && j.detail) || "bad response");
  } catch (e) {
    toast("Folder picker error: " + e, true);
    return;
  }
  dm.dir = j.dir;
  dm.parent = j.parent;
  dm.exists = !!j.exists;
  renderCrumbs();
  renderDirList(j);
}

function openDirModal() {
  $("#dirmodal").classList.remove("hidden");
  dmLoad($("#output").value.trim());
}

function closeDirModal() {
  $("#dirmodal").classList.add("hidden");
}

/* ---------- stream ---------- */
function openStream() {
  if (state.es) state.es.close();
  state.es = new EventSource("/api/stream");
  state.es.onmessage = (e) => onSnap(JSON.parse(e.data));
  state.es.onerror = () => { /* EventSource auto-reconnects */ };
}

/* ---------- wiring ---------- */
(async function init() {
  const dz = $("#dropzone");
  const fileInp = $("#file");

  dz.addEventListener("click", () => { if (!busy()) fileInp.click(); });
  fileInp.addEventListener("change", () => upload(fileInp.files[0]));
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => {
    if (busy()) return;
    upload(e.dataTransfer.files[0]);
  });

  $("#bAll").addEventListener("click", () => { if (!busy()) setSel(allIdx()); });
  $("#bNone").addEventListener("click", () => { if (!busy()) setSel(new Set()); });
  $("#bRange").addEventListener("click", () => {
    if (busy() || !state.book) return;
    const total = state.book.chapters.length;
    let a = parseInt($("#rA").value, 10), b = parseInt($("#rB").value, 10);
    if (!a || !b || a < 1 || b < 1 || a > total || b > total) {
      toast("Enter a valid range 1–" + total, true); return;
    }
    const [lo, hi] = [Math.min(a, b), Math.max(a, b)];
    setSel(new Set(state.book.chapters.slice(lo - 1, hi).map((c) => c.index)));
  });
  $("#startbtn").addEventListener("click", () => {
    if (busy()) stop(); else start();
  });

  // folder picker
  $("#browseBtn").addEventListener("click", openDirModal);
  $("#dmClose").addEventListener("click", closeDirModal);
  $("#dmCancel").addEventListener("click", closeDirModal);
  $("#dmSelect").addEventListener("click", () => {
    $("#output").value = dm.dir;
    closeDirModal();
    toast("Output location set → " + dm.dir);
  });
  $("#dirmodal").addEventListener("click", (e) => {
    if (e.target.id === "dirmodal") closeDirModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#dirmodal").classList.contains("hidden")) closeDirModal();
  });

  // populate voices
  try {
    const meta = await fetch("/api/meta").then((r) => r.json());
    const vs = $("#voice");
    meta.voices.forEach((v) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = v;
      vs.appendChild(o);
    });
    vs.value = meta.default_voice;
  } catch (e) { /* ignore */ }

  // restore state (e.g. page refresh mid-job)
  try {
    const s = await fetch("/api/snapshot").then((r) => r.json());
    state.snap = s;
    if (s.chapters.length) {
      state.book = { name: s.epub_name, chapters: s.chapters };
      selection.clear();
      (s.selected || []).forEach((i) => selection.add(i));
      $("#dzTitle").textContent = "BOOK LOADED";
      renderBookMeta();
    }
    renderChapters();
    renderStatus();
    renderProgress();
    renderConsole();
  } catch (e) { /* ignore */ }

  openStream();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Kokoro Audiobook Studio (web GUI)")
    ap.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=7860, help="Port (default 7860)")
    args = ap.parse_args()

    print(f"🚀 Kokoro Audiobook Studio → http://{args.host}:{args.port}")
    print("   Press Ctrl+C to stop the server.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
