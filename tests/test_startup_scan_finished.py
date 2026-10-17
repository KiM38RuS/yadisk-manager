# -*- coding: utf-8 -*-
"""
Регресс-тест: двойной вызов finished у StartupScanThread.

Баг (v0.12.2): StartupScanThread объявлял собственный `finished = Signal()`,
затеняя встроенный QThread.finished той же сигнатуры. В run() сигнал эмитился
явно (self.finished.emit()), а Qt дополнительно эмитил собственный finished
при завершении потока → слот _on_startup_scan_done вызывался ДВАЖДЫ →
_init_watcher() дважды → два наблюдателя watchdog → каждое файловое событие
обрабатывалось дважды (подтверждено в логах на обеих машинах).

Контракт: слот, подключённый к finished потока, вызывается ровно один раз.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import db as db_module  # noqa: E402
from ui_threads import StartupScanThread  # noqa: E402


def _make_app():
    # QApplication (не QCoreApplication) — иначе widget-тесты после зависают
    return QApplication.instance() or QApplication([])


class TestStartupScanFinishedOnce(unittest.TestCase):
    """Слот finished должен вызываться ровно один раз за жизнь потока."""

    def setUp(self):
        self.app = _make_app()
        self.database = db_module.Database(":memory:")
        self.database._init_schema()
        self.database._read_conn = self.database._conn

    def tearDown(self):
        self.database.close()

    def _run_thread(self, cache_dir):
        """Запускает реальный StartupScanThread, возвращает число вызовов finished.

        DirectConnection + thread.wait() вместо QEventLoop: в полном прогоне
        общий event loop засорён очередями и таймерами предыдущих тестов
        (утёкшие freeze-monitor/MainWindow), из-за чего queued-доставка
        finished не гарантирована за разумное время. DirectConnection вызывает
        слот синхронно в потоке эмитера — обход загрязнённого loop, при этом
        двойной emit (затенение QThread.finished) по-прежнему даёт 2 вызова.
        """
        thread = StartupScanThread(api=None, database=self.database,
                                   cache_dir=cache_dir)
        calls = []
        thread.finished.connect(lambda: calls.append(time.monotonic()),
                                Qt.DirectConnection)
        thread.start()
        ok = thread.wait(30000)
        self.assertTrue(ok, "Поток не завершился за 30 с")
        return len(calls), thread

    def test_finished_slot_called_exactly_once(self, tmp_path=None):
        import tempfile
        cache = tempfile.mkdtemp(prefix="ydm_scan_test_")
        try:
            calls, thread = self._run_thread(cache)
            self.assertEqual(
                calls, 1,
                f"Слот finished вызван {calls} раз(а) вместо 1 — "
                f"затенение QThread.finished даёт двойной вызов "
                f"(двойной _init_watcher → два наблюдателя)")
        finally:
            import shutil
            shutil.rmtree(cache, ignore_errors=True)

    def test_thread_not_leaked_after_finish(self):
        import tempfile
        cache = tempfile.mkdtemp(prefix="ydm_scan_test_")
        try:
            _calls, thread = self._run_thread(cache)
            self.assertFalse(thread.isRunning(),
                             "Поток должен завершиться после finished")
        finally:
            import shutil
            shutil.rmtree(cache, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
