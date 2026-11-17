"""Ctrl+F — фокус в строку поиска (PLANS 1.7).

Проверяем не только «где-то есть QKeySequence("Ctrl+F")», а что действие
реально живёт в окне, доступно отовсюду и действительно доходит до
обработчика:

  * у `_shortcut_search` шорткат ровно Ctrl+F (portable-текст, а не
    платформенный native-вид — иначе тест развалится на другой ОС);
  * контекст — `Qt.WindowShortcut`: это и есть гарантия «работает из
    любого виджета окна» (членство в `actions()` само по себе её не даёт);
  * действие добавлено в `MainWindow.actions()`, то есть Qt его видит
    на уровне окна, а не только у какого-то одного виджета;
  * действие включено (выключенный QAction молча перестаёт срабатывать);
  * `triggered` реально привязан к `_focus_search` — иначе Ctrl+F «есть»,
    но ничего не делает, и все проверки выше остаются зелёными;
  * `_focus_search` действительно переводит фокус и выделяет текст —
    иначе печатать поверх старого запроса нельзя.
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
    """Настоящее окно + мок обработчика Ctrl+F.

    QAction живёт только в реальном QWidget: MagicMock(QMainWindow) не может
    держать QAction-ы, а `triggered.connect` привязывается к тому, что лежит
    в классе на момент конструирования, — поэтому `_focus_search` подменяем
    ДО создания окна и получаем настоящую проводку сигнала.

    Патчим также стартовые шаги, ненужные для шорткатов: сеть/трей/вотчер,
    первичную загрузку и `_check_cache_dir` (при `cache_dir=""` он уходит в
    реальный `%USERPROFILE%\\.yadisk-cache` и на чистом профиле может открыть
    модальный `msg.exec()` — тест перестал бы быть герметичным).
    Потоки окна гасит autouse-фикстура conftest'а после теста.
    """
    import ui as ui_mod

    api = MagicMock()
    database = MagicMock()
    database.get_dir_tree_levels.return_value = ([], set())
    focus_mock = MagicMock()
    with patch.object(ui_mod.MainWindow, "_focus_search", focus_mock), \
         patch.object(ui_mod.MainWindow, "_initial_load"), \
         patch.object(ui_mod.MainWindow, "_init_updates"), \
         patch.object(ui_mod.MainWindow, "_init_tray"), \
         patch.object(ui_mod.MainWindow, "_init_watcher"), \
         patch.object(ui_mod.MainWindow, "_check_cache_dir"):
        window = ui_mod.MainWindow(api, database, cache_dir="")
    yield window, focus_mock
    # Осознанная уборка: сначала обезвредить действие, потом отдать окно
    # conftest'у (он остановит таймеры и потоки).
    window._shortcut_search.setEnabled(False)
    window.deleteLater()


def test_ctrl_f_action_registered(real_window):
    """Ctrl+F есть в окне, включён, оконный по контексту и ведёт в поиск."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence

    window, focus_mock = real_window
    action = window._shortcut_search

    # PortableText: "Ctrl+F" на всех платформах (native на macOS — "⌘F").
    assert action.shortcut().toString(
        QKeySequence.PortableText) == "Ctrl+F"
    # WindowShortcut — «доступно из любого виджета этого окна».
    assert action.shortcutContext() == Qt.WindowShortcut
    # Действие принадлежит окну — Qt найдёт его из любого дочернего виджета.
    assert action in window.actions()
    assert action.isEnabled()

    # Проводка: trigger() эмитит сигнал синхронно, окно показывать не нужно.
    # Если строку `triggered.connect(self._focus_search)` убрать, мок не
    # вызовется ни разу и тест упадёт.
    action.trigger()
    focus_mock.assert_called_once()


def test_focus_search_selects_text(qapp):
    """_focus_search переводит фокус и выделяет текущий текст."""
    import ui as ui_mod

    mw = MagicMock()
    ui_mod.MainWindow._focus_search(mw)

    mw._search_edit.setFocus.assert_called_once()
    mw._search_edit.selectAll.assert_called_once()
