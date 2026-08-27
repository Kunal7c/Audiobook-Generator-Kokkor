# ---------------------------------------------------------------------------
# studio/ — Kokoro Audiobook Studio application package
# Layout (one file per concern — see techspec.md §2 for the file map):
#   app.py     FastAPI instance + static UI serving
#   state.py   Job state + worker thread
#   api.py     /api endpoints
#   main.py    uvicorn entry point (gui.py is a thin shim into here)
#   ui/        index.html, style.css, app.js (served at / and /static)
# ---------------------------------------------------------------------------
