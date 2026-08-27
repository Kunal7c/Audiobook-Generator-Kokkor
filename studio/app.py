# ---------------------------------------------------------------------------
# studio/app.py — application instance + static UI serving
# Responsibility: the single FastAPI app, dir constants (BASE_DIR, UPLOAD_DIR,
# UI_DIR), GET / → ui/index.html, and the /static mount.
# api.py imports `app` from here and registers endpoints on it.
# ---------------------------------------------------------------------------
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
UI_DIR = Path(__file__).resolve().parent / "ui"

app = FastAPI(title="Kokoro Audiobook Studio")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(UI_DIR / "index.html")


app.mount("/static", StaticFiles(directory=UI_DIR), name="static")
