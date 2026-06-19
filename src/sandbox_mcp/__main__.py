"""Entry point: start the GC loop and serve the ASGI app with uvicorn."""

import logging
import sys
import threading
import time

import uvicorn

from .config import config
from . import sandboxes
from .server import build_app

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


def _gc_loop():
    while True:
        try:
            sandboxes.gc_once()
        except Exception:
            pass
        time.sleep(config.GC_INTERVAL_SECONDS)


def main():
    if not config.TOKEN:
        sys.exit("SMCP_TOKEN is required (set it in the environment / .env)")
    threading.Thread(target=_gc_loop, daemon=True).start()
    uvicorn.run(build_app(), host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
