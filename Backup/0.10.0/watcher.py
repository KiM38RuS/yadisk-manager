"""
Watchdog-наблюдатель за локальной папкой скачанных файлов.
При изменении файла → сигнал, который запускает загрузку в облако.
При создании/удалении файла → сигнал для синхронизации.
"""

import os
import time
import logging
from typing import Callable

from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEventHandler,
    FileModifiedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileMovedEvent,
)

logger = logging.getLogger(__name__)

# Задержка перед отправкой изменения (кумуляция автосохранений редакторов)
DEBOUNCE_SEC = 1.0


class _DebouncedHandler(FileSystemEventHandler):
    """Собирает события и через DEBOUNCE_SEC вызывает колбэк."""

    def __init__(self, callback: Callable[..., None]):
        """
        callback(event_type: str, path: str)
        event_type: 'modified', 'created', 'deleted', 'moved'
        """
        super().__init__()
        self._callback = callback
        self._pending: dict[str, tuple[str, float]] = {}
        # moved: (src, dst) → dst
        self._pending_moved: dict[str, tuple[str, str, float]] = {}
        self._last_check = 0.0

    def _is_temp(self, path: str) -> bool:
        name = os.path.basename(path)
        return name.startswith(".") or name.startswith("~") or name.endswith(".tmp")

    def on_modified(self, event: FileModifiedEvent):
        if event.is_directory or self._is_temp(event.src_path):
            return
        self._pending[event.src_path] = ("modified", time.time())
        self._schedule_flush()

    def on_created(self, event: FileCreatedEvent):
        if event.is_directory or self._is_temp(event.src_path):
            return
        # Если файл только что был создан — подождём debounce,
        # чтобы редактор успел дописать содержимое (save-as и т.п.)
        self._pending[event.src_path] = ("created", time.time())
        self._schedule_flush()

    def on_deleted(self, event: FileDeletedEvent):
        if self._is_temp(event.src_path):
            return
        if event.is_directory:
            # Удаление папки — отправляем как is_directory для массовой очистки
            self._callback("deleted", event.src_path, True)
            return
        # Удаление файла — отправляем сразу, без debounce
        logger.info("File deleted: %s", event.src_path)
        self._callback("deleted", event.src_path, False)

    def on_moved(self, event: FileMovedEvent):
        if event.is_directory or self._is_temp(event.dest_path):
            return
        # Переименование: запоминаем src→dst
        self._pending_moved[event.dest_path] = (event.src_path, event.dest_path, time.time())
        self._schedule_flush()

    def _schedule_flush(self):
        now = time.time()
        if now - self._last_check > 0.5:
            self._last_check = now
            self._flush()

    def _flush(self):
        now = time.time()

        # Сначала обрабатываем moved
        ready_moved = [v for v in self._pending_moved.values()
                       if now - v[2] >= DEBOUNCE_SEC]
        for src, dst, _ in ready_moved:
            del self._pending_moved[dst]
            logger.info("File moved: %s → %s", src, dst)
            self._callback("moved", src, dst)

        # Потом modified/created
        ready = [(p, t) for p, (t, ts) in self._pending.items()
                 if now - ts >= DEBOUNCE_SEC]
        for p, ev_type in ready:
            if os.path.exists(p):
                del self._pending[p]
                logger.info("File %s (debounced): %s", ev_type, p)
                self._callback(ev_type, p)
            else:
                # Файл исчез до окончания debounce — удаляем из pending
                logger.debug("File gone before flush: %s", p)
                del self._pending[p]


class FileWatcher:
    """Наблюдатель за папкой. Запускается в отдельном потоке (watchdog)."""

    def __init__(self, watch_dir: str,
                 on_file_changed: Callable[..., None]):
        """
        callback(event_type: str, path: str, is_directory: bool = False[, dest: str])
        event_type: 'modified' | 'created' | 'deleted' | 'moved'
        """
        self._watch_dir = watch_dir
        self._handler = _DebouncedHandler(on_file_changed)
        self._observer = Observer()

    def start(self):
        os.makedirs(self._watch_dir, exist_ok=True)
        self._observer.schedule(self._handler, self._watch_dir, recursive=True)
        self._observer.start()
        logger.info("File watcher started on %s (recursive)", self._watch_dir)

    def stop(self):
        self._observer.stop()
        self._observer.join()
        logger.info("File watcher stopped")
