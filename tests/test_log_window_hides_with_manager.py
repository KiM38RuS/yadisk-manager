"""Инбокс PLANS: окно лога должно скрываться при закрытии окна Менеджера
(а не только при сворачивании), и возвращаться при показе окна из трея.
"""

import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _window(log_visible=True):
    import ui as ui_mod

    mw = MagicMock()
    mw._log_window = MagicMock()
    mw._log_window.isVisible.return_value = log_visible
    mw._log_visible_before_minimize = False
    mw._first_close_hint = False
    mw._force_close = False
    # MagicMock подменяет методы-заглушки — связываем реальные хелперы
    mw._hide_log_with_manager = lambda: ui_mod.MainWindow._hide_log_with_manager(mw)
    mw._restore_log_with_manager = lambda: ui_mod.MainWindow._restore_log_with_manager(mw)
    return mw


def test_close_hides_log_window(qapp, monkeypatch):
    import db
    import ui
    monkeypatch.setattr(db, "get_save_window_geometry", lambda: False)

    mw = _window()
    event = MagicMock()
    ui.MainWindow.closeEvent(mw, event)

    mw._log_window.hide.assert_called_once_with()
    mw.hide.assert_called_once_with()
    event.ignore.assert_called_once_with()
    assert mw._log_visible_before_minimize is True


def test_tray_show_restores_log_window(qapp):
    import ui

    mw = _window(log_visible=False)
    mw._log_visible_before_minimize = True
    ui.MainWindow._tray_show(mw)

    mw.show.assert_called_once_with()
    mw._log_window.show.assert_called_once_with()
    mw._log_window.raise_.assert_called_once_with()
    assert mw._log_visible_before_minimize is False


def test_force_close_does_not_touch_log_window(qapp, monkeypatch):
    """Выход из программы: лог закроется вместе со всем приложением."""
    import ui

    mw = _window()
    mw._force_close = True
    event = MagicMock()
    ui.MainWindow.closeEvent(mw, event)

    mw._log_window.hide.assert_not_called()
    event.accept.assert_called_once_with()
