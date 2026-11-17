"""Ctrl+F — фокус в строку поиска (PLANS 1.7).

Проверяем не только «где-то есть QKeySequence("Ctrl+F")», а что действие
реально живёт в окне и доступно отовсюду:

  * у `_shortcut_search` шорткат ровно Ctrl+F (portable-текст, а не
    платформенный native-вид — иначе тест развалится на другой ОС);
  * действие добавлено в `MainWindow.actions()`, то есть Qt его видит
    на уровне окна, а не только у какого-то одного виджета;
  * действие включено (выключенный QAction молча перестаёт срабатывать);
  * `_focus_search` действительно переводит фокус и выделяет текст —
    иначе Ctrl+F «срабатывает», но печатать поверх старого запроса нельзя.
"""
import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def real_window(qapp):
    """Настоящее окно: QAction живёт только в реальном QWidget.

    MagicMock(QMainWindow) не может держать QAction-и в `actions()`,
    поэтому проверяем на реальном окне. Патчим четыре стартовых шага,
    которые не нужны для шорткатов (сеть/трей/вотчер/первичная загрузка);
    потоки окна гасит autouse-фикстура conftest'а после теста.
    """
    import ui as ui_mod

    api = MagicMock()
    database = MagicMock()
    database.get_dir_tree_levels.return_value = ([], set())
    with patch.object(ui_mod.MainWindow, "_initial_load"), \
         patch.object(ui_mod.MainWindow, "_init_updates"), \
         patch.object(ui_mod.MainWindow, "_init_tray"), \
         patch.object(ui_mod.MainWindow, "_init_watcher"):
        window = ui_mod.MainWindow(api, database, cache_dir="")
    yield window
    # Осознанная уборка: сначала обезвредить действие, потом отдать окно
    # conftest'у (он остановит таймеры и потоки).
    window._shortcut_search.setEnabled(False)
    window.deleteLater()


def test_ctrl_f_action_registered(real_window):
    """Ctrl+F есть в окне, включён и не зависит от платформы."""
    from PySide6.QtGui import QKeySequence

    window = real_window
    action = window._shortcut_search

    # PortableText: "Ctrl+F" на всех платформах (native на macOS — "⌘F").
    assert action.shortcut().toString(
        QKeySequence.PortableText) == "Ctrl+F"
    # Действие принадлежит окну — Qt найдёт его из любого дочернего виджета.
    assert action in window.actions()
    assert action.isEnabled()


def test_focus_search_selects_text(qapp):
    """_focus_search переводит фокус и выделяет текущий текст."""
    import ui as ui_mod

    mw = MagicMock()
    ui_mod.MainWindow._focus_search(mw)

    mw._search_edit.setFocus.assert_called_once()
    mw._search_edit.selectAll.assert_called_once()
