"""Лог в %APPDATA%\\SoundpadMusic\\log.txt — у exe нет консоли, иначе ошибки не видно."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading

log = logging.getLogger("soundpad_music")
_path = None


def path() -> str:
    global _path
    if _path is None:
        from .player import app_dir
        _path = os.path.join(app_dir(), "log.txt")
    return _path


def setup() -> None:
    if log.handlers:
        return
    log.setLevel(logging.DEBUG)
    try:
        h = logging.handlers.RotatingFileHandler(path(), maxBytes=1_000_000, backupCount=1,
                                                 encoding="utf-8")
    except OSError:
        return
    h.setFormatter(logging.Formatter("%(asctime)s [%(threadName)s] %(levelname)s %(message)s"))
    log.addHandler(h)

    def thread_hook(args):
        log.error("Необработанная ошибка в потоке %s", args.thread.name if args.thread else "?",
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = thread_hook

    def sys_hook(*exc):
        log.error("Необработанная ошибка", exc_info=exc)
    sys.excepthook = sys_hook
