"""Клавиатура в дереве папок: F2 — переименовать, Enter — открыть (PLANS 1.6).

Ключевые ограничения:
  * F2 на корне («Яндекс Диск», "/") не должен вызывать _rename_item —
    переименование корня попыталось бы переместить весь Диск;
  * Enter возвращает True только когда навигация реально произошла, иначе
    QTreeView должен сам развернуть/свернуть узел;
  * всё остальное (стрелки, type-ahead, expand/collapse) не потребляем.
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


def _key(key):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    return QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier)


def _row(user_role_data):
    idx = MagicMock()
    idx.data.return_value = user_role_data
    return idx


def _tree_window(rows, item=None):
    """MainWindow-заглушка: только то, что трогают обработчики дерева."""
    mw = MagicMock()
    mw.tree_view.selectionModel().selectedRows.return_value = rows
    mw.tree_model._find_item.return_value = item
    return mw


@pytest.fixture
def real_window(qapp):
    """Настоящее окно: хвост eventFilter (`super().eventFilter`) требует QWidget.

    MagicMock не проходит проверку isinstance в super() и не может быть
    передан в QMainWindow.eventFilter — поэтому проводку фильтра проверяем
    на реальном окне (conftest гасит его потоки после теста).
    """
    import ui as ui_mod
    window = ui_mod.MainWindow(MagicMock(), MagicMock(), cache_dir="")
    yield window


# ── хелперы ────────────────────────────────────────────────

def test_f2_renames_selected_tree_item(qapp):
    import ui as ui_mod
    from PySide6.QtCore import Qt

    mw = MagicMock()
    handled = ui_mod.MainWindow._handle_tree_key(mw, _key(Qt.Key_F2))

    assert handled is True
    mw._rename_tree_selected.assert_called_once()


def test_enter_opens_selected_tree_folder(qapp):
    import ui as ui_mod
    from PySide6.QtCore import Qt

    mw = _tree_window([_row("/Photos")], MagicMock(cloud_path="/Photos"))

    handled = ui_mod.MainWindow._handle_tree_key(mw, _key(Qt.Key_Return))

    assert handled is True
    mw._navigate_to_folder.assert_called_once_with("/Photos")


def test_f2_in_tree_calls_rename_item(qapp):
    import ui as ui_mod

    item = MagicMock(cloud_path="/Photos")
    item.name = "Photos"
    mw = _tree_window([_row("/Photos")], item)

    ui_mod.MainWindow._rename_tree_selected(mw)

    mw._rename_item.assert_called_once()
    assert mw._rename_item.call_args.args[0] == "/Photos"
    assert mw._rename_item.call_args.args[1] == "Photos"
    assert mw._rename_item.call_args.args[2] == "dir"


# ── не потребляем чужие клавиши ────────────────────────────

def test_unhandled_key_is_not_consumed(qapp):
    import ui as ui_mod
    from PySide6.QtCore import Qt

    mw = MagicMock()
    handled = ui_mod.MainWindow._handle_tree_key(mw, _key(Qt.Key_Left))

    assert handled is False
    mw._rename_tree_selected.assert_not_called()
    mw._navigate_to_folder.assert_not_called()


# ── корень защищён ─────────────────────────────────────────

def test_f2_on_root_is_ignored(qapp):
    import ui as ui_mod
    from PySide6.QtCore import Qt

    root = MagicMock(cloud_path="/")
    root.name = "Яндекс Диск"
    mw = _tree_window([_row("/")], root)

    # Клавишу потребляем (иначе Qt пищит на необработанный F2)...
    assert ui_mod.MainWindow._handle_tree_key(mw, _key(Qt.Key_F2)) is True

    # ...но переименования корня не происходит (корень не трогаем).
    ui_mod.MainWindow._rename_tree_selected(mw)
    mw._rename_item.assert_not_called()


# ── Enter без навигации не потребляется ────────────────────

def test_enter_without_selection_is_not_consumed(qapp):
    import ui as ui_mod
    from PySide6.QtCore import Qt

    mw = _tree_window([], None)

    handled = ui_mod.MainWindow._handle_tree_key(mw, _key(Qt.Key_Return))

    assert handled is False
    mw._navigate_to_folder.assert_not_called()


def test_enter_on_root_is_not_consumed(qapp):
    import ui as ui_mod
    from PySide6.QtCore import Qt

    mw = _tree_window([_row("/")], MagicMock(cloud_path="/"))

    handled = ui_mod.MainWindow._handle_tree_key(mw, _key(Qt.Key_Return))

    assert handled is False
    mw._navigate_to_folder.assert_not_called()


def test_enter_on_unknown_item_is_not_consumed(qapp):
    import ui as ui_mod
    from PySide6.QtCore import Qt

    mw = _tree_window([_row("/Gone")], None)

    handled = ui_mod.MainWindow._handle_tree_key(mw, _key(Qt.Key_Return))

    assert handled is False
    mw._navigate_to_folder.assert_not_called()


# ── проводка в eventFilter ─────────────────────────────────

def test_event_filter_routes_tree_keypress_to_handler(real_window):
    from PySide6.QtCore import QEvent
    from PySide6.QtCore import Qt

    window = real_window
    window._handle_tree_key = MagicMock(return_value=True)

    key_event = _key(Qt.Key_F2)
    assert window.eventFilter(window.tree_view, key_event) is True
    window._handle_tree_key.assert_called_once_with(key_event)

    # не-KeyPress (например Enter/Leave) в обработчик клавиш не попадает
    window._handle_tree_key.reset_mock()
    assert window.eventFilter(window.tree_view, QEvent(QEvent.Enter)) is False
    window._handle_tree_key.assert_not_called()
