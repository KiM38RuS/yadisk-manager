"""
Тесты безопасной проверки живого QThread (_is_thread_alive).

Защита от CRITICAL-шторма в _process_result_queue (см. PLANS.md 2.14):
shiboken6 падает с 'Internal C++ object (...) already deleted', если тред
собран GC, но ссылка осталась в self._active_threads. _is_thread_alive
оборачивает t.isRunning() в try/except RuntimeError и возвращает False
для удалённого C++ объекта — такой тред можно безопасно убрать из списка.

Эти тесты намеренно НЕ требуют работающего QApplication:
- None → False (защита от пустого слота)
- объект без isRunning → False (имитация удалённого C++)
- объект с isRunning() → True (нормальный путь)
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ui.py импортирует PySide6 — ставим offscreen ДО импорта
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui import _is_thread_alive


class _FakeDeletedThread:
    """Имитация QThread, у которого C++ объект уже удалён (shiboken6 бросает RuntimeError)."""

    def isRunning(self):  # noqa: N802 — соответствует Qt API
        raise RuntimeError(
            "Internal C++ object (QThread) already deleted."
        )


class _FakeRunningThread:
    """Имитация живого QThread, который ещё работает."""
    def isRunning(self):  # noqa: N802
        return True


class _FakeFinishedThread:
    """Имитация QThread, который штатно завершился."""
    def isRunning(self):  # noqa: N802
        return False


class TestIsThreadAlive(unittest.TestCase):
    def test_none_returns_false(self):
        """None-ссылка (пустой слот) не должна падать — это не живой тред."""
        self.assertFalse(_is_thread_alive(None))

    def test_alive_thread_returns_true(self):
        """Живой тред (isRunning()==True) определяется как живой."""
        self.assertTrue(_is_thread_alive(_FakeRunningThread()))

    def test_finished_thread_returns_false(self):
        """Штатно завершившийся тред (isRunning()==False) — мёртв."""
        self.assertFalse(_is_thread_alive(_FakeFinishedThread()))

    def test_deleted_cpp_object_returns_false(self):
        """Удалённый C++ объект → RuntimeError → False (НЕ пробрасывается)."""
        # До фикса этот вызов поднимал RuntimeError и сыпал CRITICAL в лог
        # каждые 100 мс через _process_result_queue. Теперь — тихий False.
        result = _is_thread_alive(_FakeDeletedThread())
        self.assertFalse(result)

    def test_deleted_cpp_object_no_exception(self):
        """Поверка: никакое исключение не утекает наружу."""
        try:
            _is_thread_alive(_FakeDeletedThread())
        except RuntimeError as e:
            self.fail(f"RuntimeError утекла наружу: {e}")

    def test_returns_bool(self):
        """Возвращаемое значение — ровно bool (не int 0/1, не None)."""
        self.assertIsInstance(_is_thread_alive(_FakeRunningThread()), bool)
        self.assertIsInstance(_is_thread_alive(_FakeFinishedThread()), bool)
        self.assertIsInstance(_is_thread_alive(_FakeDeletedThread()), bool)
        self.assertIsInstance(_is_thread_alive(None), bool)

    def test_works_with_magic_mock_raising_runtimeerror(self):
        """MagicMock-объект, настроенный бросать RuntimeError — тоже False."""
        mock = MagicMock()
        mock.isRunning.side_effect = RuntimeError("already deleted")
        self.assertFalse(_is_thread_alive(mock))


if __name__ == "__main__":
    unittest.main()
