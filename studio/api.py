# ---------------------------------------------------------------------------
# studio/api.py — /api endpoints
# Responsibility: the HTTP surface only (meta, dirs, book upload, start,
# stop, snapshot, SSE stream). Job mutation goes through state.py; the
# `GET /` route and static files live in app.py.
# ---------------------------------------------------------------------------
"""FastAPI endpoints (verbatim from legacy gui.py L224-405)."""
import hashlib
import json
import threading
import time
from pathlib import Path

from fastapi import File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from tts import AVAILABLE_VOICES, get_chapters, KPipeline, synthesise_chapter

from .app import app, BASE_DIR, UPLOAD_DIR
from .state import job, log, run_job, snapshot


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


# --- Voice preview (stateless — techspec §3.3) ----------------------------
# Never touches `job`: a plain sync endpoint runs in FastAPI's thread pool,
# one module-level lock serialises preview synthesis, and the WAV is cached
# in preview_cache/ (project root) so repeat requests are served instantly.
PREVIEW_CACHE_DIR = BASE_DIR / "preview_cache"
_preview_lock = threading.Lock()
_preview_pipeline: dict[str, KPipeline] = {}


class PreviewReq(BaseModel):
    text: str
    voice: str
    lang: str


@app.post("/api/preview")
def api_preview(req: PreviewReq):
    """Synthesise arbitrary user text (not chapters) and return the WAV."""
    if not req.text.strip():
        raise HTTPException(400, "Text must not be empty.")
    if req.voice not in AVAILABLE_VOICES:
        raise HTTPException(400, f"Unknown voice: {req.voice}")
    if req.lang not in ("a", "b"):
        raise HTTPException(400, "Language must be 'a' or 'b'.")

    filename = (
        "preview_"
        + hashlib.sha1(f"{req.text}|{req.voice}|{req.lang}".encode()).hexdigest()[:8]
        + ".wav"
    )
    cache_path = PREVIEW_CACHE_DIR / filename

    if not cache_path.is_file():
        with _preview_lock:
            pipeline = _preview_pipeline.get(req.lang)
            if pipeline is None:
                pipeline = KPipeline(lang_code=req.lang)
                _preview_pipeline[req.lang] = pipeline
            tmp_dir = PREVIEW_CACHE_DIR / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            wav_path = synthesise_chapter(
                pipeline, req.text, req.voice, tmp_dir, 0, verbose=False
            )
            if wav_path is None:
                raise HTTPException(500, "No audio generated for that text.")
            Path(wav_path).replace(cache_path)
            for seg in tmp_dir.glob("seg_*.wav"):
                seg.unlink(missing_ok=True)

    return FileResponse(cache_path, media_type="audio/wav", filename=filename)

