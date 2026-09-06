from __future__ import annotations

import asyncio
import logging
import sys

from .app import main

if __name__ == "__main__":
    # Without a root handler, aiogram/telethon/uvicorn errors reach stderr only
    # through logging.lastResort (unformatted, WARNING+). journald then shows
    # handler exceptions with no timestamp or logger name.
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
