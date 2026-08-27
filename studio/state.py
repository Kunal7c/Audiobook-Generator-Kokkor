# ---------------------------------------------------------------------------
# studio/state.py — job state + worker thread
# Responsibility: the single synthesis job (class Job, module-level job),
# job log + snapshot(), progress callback (make_cb), worker thread (run_job).
# Mutations are protected by job._lock. No endpoints, no UI — see §2.
# ---------------------------------------------------------------------------
"""Job state and worker thread (verbatim from legacy gui.py L50-218)."""
import threading
import time
from pathlib import Path

from tts import StopRequested, synthesise_chapter


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

