"""File-rotating logging setup.

Daily rotation at midnight, 7-day retention. Logs go to both stderr (so the
dev console shows them) and a file under the configured log dir. Uvicorn's
own loggers are wired through ours so HTTP access and errors land in the
same file as our app messages.

Call `configure_logging()` once at process start, before any other code logs.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from backend.app.config import get_settings

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def configure_logging() -> None:
    settings = get_settings()
    log_dir = Path(settings.log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=log_dir / "pa.log",
        when="midnight",
        backupCount=7,
        encoding="utf-8",
        utc=False,
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # Map the configured name (e.g. "INFO") to logging's integer constant; fall back if invalid.
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    # Clear any handlers a parent (uvicorn, pytest) installed so our config wins.
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.setLevel(level)

    # Uvicorn ships its own handlers on these named loggers; route them through
    # ours instead so /chat traffic and our app logs end up in the same file.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
