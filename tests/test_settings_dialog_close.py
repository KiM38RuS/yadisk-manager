"""
Тесты: защита от STATUS_HEAP_CORRUPTION при быстром клике OK после
смены темы в настройках (v0.12.1).

Проблема: пользователь меняет тему в комбобоксе → _on_theme_changed
планирует parent._apply_theme через singleShot(0). Если пользователь
сразу нажимает OK, _save() → accept() запускает уничтожение виджетов
диалога. Отложенный singleShot(0, _apply_theme) срабатывает во время
destroy-цепочки, вызывает app.setStyleSheet() и повторно
currentIndexChanged на уже-умирающем QComboBox → повреждение кучи Qt
(процесс завершается без traceback, main.py «вылетает без ошибки»).

Защита: accept()/reject()/closeEvent() выставляют self._closing=True
и отключают currentIndexChanged. _on_theme_changed при _closing=True
ничего не делает. db.set_theme уже вызван, тема применится при
следующем запуске или через singleShot, если успеет до _closing.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import db
import disk_api
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog

import ui_dialogs


def _make_app():
    """Вернуть существующий QApplication или создать новый."""
    return QApplication.instance() or QApplication([])


def _make_main_window():
    """Создать MainWindow в offscreen-режиме (без сетевых вызовов)."""
    database = db.Database(":memory:")
    api = disk_api.YaDiskAPI.__new__(disk_api.YaDiskAPI)
    api._session = mock.MagicMock()
    from ui import MainWindow
    return MainWindow(api, database)


class TestSettingsDialogClose(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.win = _make_main_window()
        self.dlg = ui_dialogs.SettingsDialog(self.win)

    def tearDown(self):
        try:
            self.dlg.deleteLater()
        except Exception:
            pass
        try:
            self.win.deleteLater()
        except Exception:
            pass

    def test_closing_flag_set_on_accept(self):
        """accept() устанавливает self._closing=True."""
        self.assertFalse(getattr(self.dlg, "_closing", False))
        self.dlg.accept()
        self.assertTrue(self.dlg._closing)

    def test_closing_flag_set_on_reject(self):
        """reject() устанавливает self._closing=True."""
        self.assertFalse(getattr(self.dlg, "_closing", False))
        self.dlg.reject()
        self.assertTrue(self.dlg._closing)

    def test_closing_flag_set_on_close_event(self):
        """closeEvent устанавливает self._closing=True."""
        self.assertFalse(getattr(self.dlg, "_closing", False))
        from PySide6.QtGui import QCloseEvent
        evt = QCloseEvent()
        self.dlg.closeEvent(evt)
        self.assertTrue(self.dlg._closing)
        evt.accept()

    def test_theme_signal_disconnected_on_accept(self):
        """accept() отключает currentIndexChanged комбокса.

        Проверяем через wrapper: подключаем свой слот-маркер, после
        accept() наш слот тоже должен отключиться из-за disconnect all.
        """
        called = []
        def marker():
            called.append(1)
        # Подключаем маркер к текущему сигналу
        self.dlg._theme_combo.currentIndexChanged.connect(marker)
        # accept() должен отключить ВСЕ слоты, включая наш marker
        self.dlg.accept()
        # Эмитим сигнал вручную — marker НЕ должен вызваться
        # (currentIndexChanged с тем же значением — допустимый вызов)
        idx = self.dlg._theme_combo.currentIndex()
        self.dlg._theme_combo.currentIndexChanged.emit(idx)
        self.assertEqual(called, [], "marker slot should be disconnected after accept()")

    def test_theme_changed_after_close_does_not_apply(self):
        """_on_theme_changed НЕ запускает parent._apply_theme после close.

        Симулирует ситуацию: пользователь выбрал новую тему в комбобоксе,
        потом нажал OK. _closing=True → apply не запускается.
        """
        # Закрываем диалог
        self.dlg.accept()
        self.assertTrue(self.dlg._closing)
        # Теперь эмулируем currentIndexChanged (Qt шлёт его при repolish
        # виджета, даже если значение не изменилось)
        with mock.patch.object(
                self.win, "_apply_theme", wraps=self.win._apply_theme) as spy:
            self.dlg._on_theme_changed()
            spy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
