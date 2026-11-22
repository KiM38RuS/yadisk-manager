"""Конфликт локальной и облачной версий: трей + надпись в статус-баре.

Ожидаемое поведение:
* при конфликте НЕ открывается окно Менеджера и НЕ показывается сразу диалог;
* показывается уведомление в трее и статус «ошибка синхронизации»;
* в строке состояния слева от «Показать лог» появляется кликабельная
  надпись «⚠️ Конфликт с облаком», по клику открывается диалог разрешения.
"""
import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _mock_window():
    import ui as ui_mod

    mw = MagicMock()
    mw._tray = MagicMock()
    mw._online = True
    mw._syncing = set()
    mw._active_uploads = 0
    mw._active_downloads = 0
    mw._upload_queue = []
    mw._download_queue = []
    mw._bulk_sync_running = False
    mw._pending_conflicts = []
    mw._conflict_label = MagicMock()
    mw._app_status_current = None
    # MagicMock подменяет методы — связываем реальную логику; _tray_show
    # оставляем моком, чтобы проверить, что он НЕ вызван до клика по надписи.
    mw._app_status = lambda: ui_mod.MainWindow._app_status(mw)
    mw._notify = lambda *a, **kw: ui_mod.MainWindow._notify(mw, *a, **kw)
    mw._register_conflict = lambda *a, **kw: ui_mod.MainWindow._register_conflict(mw, *a, **kw)
    mw._set_conflict_indicator = lambda *a, **kw: ui_mod.MainWindow._set_conflict_indicator(mw, *a, **kw)
    mw._refresh_app_status = lambda *a, **kw: ui_mod.MainWindow._refresh_app_status(mw, *a, **kw)
    mw._clear_pending_conflict = lambda *a, **kw: ui_mod.MainWindow._clear_pending_conflict(mw, *a, **kw)
    mw._show_conflict_dialog = lambda *a, **kw: ui_mod.MainWindow._show_conflict_dialog(mw, *a, **kw)
    return mw


def test_handle_conflict_notifies_instead_of_opening_dialog(qapp):
    """Конфликт фиксируется и уведомляет, но не поднимает окно и не открывает диалог."""
    from ui import MainWindow

    mw = _mock_window()
    with patch("ui.QGuiApplication.platformName", return_value="windows"), \
         patch("ui.QMessageBox") as message_box:
        MainWindow._handle_conflict(mw, "/docs/a.txt", "/cache/a.txt",
                                    "local123", "cloud456", cloud_size=42)

    message_box.assert_not_called()
    mw._tray_show.assert_not_called()
    assert len(mw._pending_conflicts) == 1
    assert mw._pending_conflicts[0]["cloud_path"] == "/docs/a.txt"
    title, body = mw._tray.showMessage.call_args.args[:2]
    assert title == "Конфликт синхронизации"
    assert "a.txt" in body


def test_conflict_indicator_label_text_and_visibility(qapp):
    """Надпись показывает точный текст и скрывается, когда конфликтов нет."""
    from PySide6.QtWidgets import QLabel
    from ui import MainWindow

    mw = _mock_window()
    label = QLabel()
    mw._conflict_label = label

    mw._set_conflict_indicator(True)
    assert label.text() == "⚠️ Конфликт с облаком"
    assert not label.isHidden()

    mw._set_conflict_indicator(False)
    assert label.isHidden()


def test_conflict_label_precedes_log_label_and_opens_dialog(qapp):
    """Настоящее окно: надпись слева от «Показать лог» и кликает по диалогу."""
    from PySide6.QtWidgets import QLabel
    from ui import MainWindow

    win = MainWindow(MagicMock(), MagicMock(), cache_dir="")
    layout = win._status_right_layout
    texts = [w.text() for i in range(layout.count())
             if isinstance((w := layout.itemAt(i).widget()), QLabel)]
    assert "⚠️ Конфликт с облаком" in texts
    assert texts.index("⚠️ Конфликт с облаком") < texts.index("Показать лог")

    win._register_conflict("/docs/a.txt", "/cache/a.txt",
                           "local123", "cloud456", cloud_size=42)
    with patch.object(win, "_tray_show") as tray_show, \
         patch("ui.QMessageBox") as message_box:
        win._on_conflict_label_clicked()

    tray_show.assert_called_once()
    assert message_box.call_count == 1


def test_cancel_in_dialog_keeps_conflict_indicator(qapp):
    """Отмена выбора — это не разрешение: надпись и статус остаются."""
    from ui import MainWindow

    mw = _mock_window()
    mw._register_conflict("/docs/a.txt", "/cache/a.txt",
                          "local123", "cloud456", cloud_size=42)
    yes, no, cancel = MagicMock(), MagicMock(), MagicMock()
    with patch("ui.QMessageBox") as message_box:
        message_box.return_value.addButton.side_effect = [yes, no, cancel]
        message_box.return_value.clickedButton.return_value = cancel
        mw._show_conflict_dialog(**mw._pending_conflicts[0])

    assert len(mw._pending_conflicts) == 1
    assert mw._app_status() == "conflict"


def test_resolving_conflict_clears_status_and_label(qapp):
    """После разрешения конфликта статус и надпись возвращаются в норму."""
    from ui import MainWindow

    mw = _mock_window()
    mw._register_conflict("/docs/a.txt", "/cache/a.txt",
                          "local123", "cloud456", cloud_size=42)
    assert mw._app_status() == "conflict"

    mw._clear_pending_conflict()
    assert mw._pending_conflicts == []
    assert mw._app_status() == "ready"
    mw._conflict_label.hide.assert_called()
