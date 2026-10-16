"""
Тесты защиты от дублей ApiListThread (PLANS.md 2.16).

Симптом: при быстрой навигации (или двойном сигнале) в логе появлялось
два 'ApiListThread: получение /Документы...' подряд с разницей 30 мс —
второй поток перетирал результат первого.

Фикс: MainWindow._loading_paths (set[str]) — путь, который сейчас
загружается. Повторный _fetch_folder_list для того же пути игнорируется.
После завершения потока путь удаляется из set.

Стратегия: тестируем ЛОГИКУ guard-а (set membership) без реального QThread.
Используем MagicMock с реальным атрибутом `finished` (Signal-объект),
чтобы thread.finished.connect(...) работал как в настоящем коде.
Никаких msleep, threading.Event — иначе Qt+exit падает с 0xC0000409
в комбинации с другими Qt-тестами.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication


def make_fake_thread(path, call_log):
    """Создать MagicMock, прикидывающийся _ApiListThread.

    finished — Signal (MagicMock), .connect() — MagicMock, который
    просто складывает callback в self._handlers. start() — MagicMock,
    логирует вызов в call_log.
    """
    t = MagicMock()
    t._path = path
    t._handlers = []

    def fake_connect(handler):
        t._handlers.append(handler)
        return None
    t.finished.connect = fake_connect

    def fake_start():
        call_log.append(path)
    t.start = fake_start

    def emit_finished():
        for h in t._handlers:
            try:
                h([], "")
            except TypeError:
                h()
    t._emit_finished = emit_finished

    return t


class TestLoadingPathsGuard(unittest.TestCase):
    """Защита от дублей _ApiListThread через _loading_paths."""

    @classmethod
    def setUpClass(cls):
        # QCoreApplication нужен для импорта ui.py (QMainWindow и т.п.).
        # Используем существующий инстанс, если он уже создан другим
        # тестом — это предотвращает stack-overrun при exit в комбинации
        # с test_settings_dialog_close, который тоже использует Qt.
        cls._app = QCoreApplication.instance()

    def setUp(self):
        from ui import MainWindow
        self.MainWindow = MainWindow
        self.target = MainWindow.__new__(MainWindow)
        self.target._loading_paths = set()
        self.target._api = MagicMock()
        self.started_paths = []
        self.fake_threads = []

        def fake_ctor(api, path, parent=None):
            t = make_fake_thread(path, self.started_paths)
            self.fake_threads.append(t)
            return t

        self._patcher = patch("ui._ApiListThread", side_effect=fake_ctor)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def _call_fetch(self, path, auto_emit=True):
        """Вызвать _fetch_folder_list; опционально сразу же эмитнуть finished."""
        self.MainWindow._fetch_folder_list(
            self.target, path, lambda items, err: None)
        if auto_emit and self.fake_threads:
            self.fake_threads[-1]._emit_finished()

    def test_initial_loading_paths_is_empty(self):
        self.assertEqual(self.target._loading_paths, set())

    def test_first_call_adds_path(self):
        self._call_fetch("/Документы", auto_emit=False)
        self.assertIn("/Документы", self.target._loading_paths)

    def test_duplicate_call_is_blocked(self):
        """Пока путь в _loading_paths — повторный вызов игнорируется."""
        self._call_fetch("/Документы", auto_emit=False)
        self.assertIn("/Документы", self.target._loading_paths)
        # Повторный вызов — guard должен сработать
        self._call_fetch("/Документы", auto_emit=False)
        # Количество созданных потоков для /Документы должно быть 1
        count = sum(1 for t in self.fake_threads if t._path == "/Документы")
        self.assertEqual(count, 1,
                         f"Ожидался 1 поток для /Документы, создано {count}")

    def test_different_paths_dont_block(self):
        self._call_fetch("/A", auto_emit=False)
        self._call_fetch("/B", auto_emit=False)
        self.assertIn("/A", self.target._loading_paths)
        self.assertIn("/B", self.target._loading_paths)
        self.assertEqual(len(self.target._loading_paths), 2)

    def test_path_removed_after_finished(self):
        """После finished путь удаляется из _loading_paths."""
        self._call_fetch("/X", auto_emit=False)
        self.assertIn("/X", self.target._loading_paths)
        # Эмитим finished (lambda discard)
        self.fake_threads[-1]._emit_finished()
        self.assertNotIn("/X", self.target._loading_paths)

    def test_after_finished_path_can_be_re_fetched(self):
        """После завершения первой загрузки тот же путь можно загрузить снова."""
        self._call_fetch("/Y", auto_emit=True)
        self.assertNotIn("/Y", self.target._loading_paths)
        # Повторный вызов — должен стартовать (путь уже не в set)
        self._call_fetch("/Y", auto_emit=False)
        self.assertIn("/Y", self.target._loading_paths)
        count = sum(1 for t in self.fake_threads if t._path == "/Y")
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
