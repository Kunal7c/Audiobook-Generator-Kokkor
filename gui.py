#!/usr/bin/env python3
"""
Kokoro Audiobook Studio — thin entry shim.

All implementation lives in the `studio/` package:
    studio/app.py    FastAPI instance + static UI serving
    studio/state.py  Job state + worker thread
    studio/api.py    /api endpoints
    studio/main.py   uvicorn entry point
    studio/ui/       index.html, style.css, app.js

Run:
    .venv/bin/python gui.py                # → http://127.0.0.1:7860
    .venv/bin/python gui.py --port 9000

Dependencies (already in .venv):
    fastapi, uvicorn, python-multipart
    (plus the tts.py stack: kokoro, soundfile, ebooklib, beautifulsoup4, numpy)

The CLI in tts.py is untouched — use it for headless runs.
Technical reference + live contracts: techspec.md
"""

from studio.main import main

if __name__ == "__main__":
    main()
