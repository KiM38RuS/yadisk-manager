"""
Watchdog-наблюдатель за локальной папкой скачанных файлов.
При изменении файла → сигнал, который запускает загрузку в облако.
"""

import os
import time
import logging
from typing import Callable

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent

logger = logging.getLogger(__name__)

# Задержка перед отправкой изменения (чтобы не дёргаться на каждое
# автосохранение — кумуляция изменений)
DEBOUNCE_SEC = 3.0


class _DebouncedHandler(FileSystemEventHandler):
    """Собирает события и через DEBOUNCE_SEC вызывает колбэк."""

    def __init__(self, callback: Callable[[str], None]):
        super().__init__()
        self._callback = callback
        self._pending: dict[str, float] = {}
        self._last_check = 0.0

    def on_modified(self, event: FileModifiedEvent):
        if event.is_directory:
            return
        # игнорируем временные файлы редакторов
        name = os.path.basename(event.src_path)
        if name.startswith(".") or name.endswith("~") or name.endswith(".tmp"):
            return
        self._pending[event.src_path] = time.time()
        # проверяем раз в секунду
        now = time.time()
        if now - self._last_check > 0.5:
            self._last_check = now
            self._flush()

    def _flush(self):
        now = time.time()
        ready = [p for p, t in self._pending.items()
                 if now - t >= DEBOUNCE_SEC]
        for p in ready:
            del self._pending[p]
            logger.info("File changed (debounced): %s", p)
            self._callback(p)


class FileWatcher:
    """Наблюдатель за папкой. Запускается в отдельном потоке (watchdog)."""

    def __init__(self, watch_dir: str, on_file_changed: Callable[[str], None]):
        self._watch_dir = watch_dir
        self._handler = _DebouncedHandler(on_file_changed)
        self._observer = Observer()

    def start(self):
        os.makedirs(self._watch_dir, exist_ok=True)
        self._observer.schedule(self._handler, self._watch_dir, recursive=True)
        self._observer.start()
        logger.info("File watcher started on %s", self._watch_dir)

    def stop(self):
        self._observer.stop()
        self._observer.join()
        logger.info("File watcher stopped")
