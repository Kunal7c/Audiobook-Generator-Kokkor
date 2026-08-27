# ---------------------------------------------------------------------------
# studio/main.py — uvicorn entry point
# Responsibility: parse --host/--port, print the banner, run uvicorn on the
# shared app. gui.py is a thin shim that calls main() from here.
# ---------------------------------------------------------------------------
import argparse

import uvicorn

from . import api  # noqa: F401 — registers endpoints on the shared app
from .app import app


def main() -> None:
    ap = argparse.ArgumentParser(description="Kokoro Audiobook Studio (web GUI)")
    ap.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=7860, help="Port (default 7860)")
    args = ap.parse_args()

    print(f"🚀 Kokoro Audiobook Studio → http://{args.host}:{args.port}")
    print("   Press Ctrl+C to stop the server.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
