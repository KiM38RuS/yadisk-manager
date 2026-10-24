"""PLANS 2.5 — Ctrl+A не должен выделять служебную строку «..».

Строка «..» (is_parent_nav) — навигационная, а не файл: попадая в выделение,
она ломает логику кнопок «Сохранить на компьютере» / «Оставить только в
облаке» (действие применяется к пути родителя вместо выделенных файлов).
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


def _key_event(key, modifiers, text=""):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    return QKeyEvent(QEvent.KeyPress, key, modifiers, text)


def _fake_window(items: dict[int, dict]):
    """MainWindow-заглушка: только то, что трогает выделение всех строк."""
    import ui as ui_mod
    mw = MagicMock()
    mw.table_sort_model.rowCount.return_value = len(items)
    mw.table_sort_model.index.side_effect = lambda r, c: MagicMock(row=lambda r=r: r)
    mw._get_item.side_effect = lambda idx: items.get(idx.row())
    return mw, ui_mod


def _selected_rows(mw):
    return [c.args[0].row()
            for c in mw.table_view.selectionModel().select.call_args_list]


def test_select_all_skips_parent_nav_row(qapp):
    items = {
        0: {"is_parent_nav": True, "cloud_path": "/A", "name": ".."},
        1: {"cloud_path": "/A/a.txt", "name": "a.txt"},
        2: {"cloud_path": "/A/b.txt", "name": "b.txt"},
    }
    mw, ui_mod = _fake_window(items)

    ui_mod.MainWindow._select_all_files(mw)

    assert _selected_rows(mw) == [1, 2]   # «..» (строка 0) не выделена
    mw._update_selection_counter.assert_called_once()
    mw._update_toolbar_buttons.assert_called_once()


def test_select_all_selects_everything_without_parent_nav(qapp):
    """В результатах поиска строки «..» нет — выделяется всё."""
    items = {
        0: {"cloud_path": "/a.txt", "name": "a.txt"},
        1: {"cloud_path": "/b.txt", "name": "b.txt"},
    }
    mw, ui_mod = _fake_window(items)

    ui_mod.MainWindow._select_all_files(mw)

    assert _selected_rows(mw) == [0, 1]


def test_ctrl_a_routes_to_select_all(qapp):
    from PySide6.QtCore import Qt
    mw, ui_mod = _fake_window({0: {"cloud_path": "/a.txt"}})

    handled = ui_mod.MainWindow._handle_table_key(
        mw, _key_event(Qt.Key_A, Qt.ControlModifier))

    assert handled is True
    mw._select_all_files.assert_called_once()


def test_plain_a_still_typeahead(qapp):
    """Без Ctrl «a» остаётся type-ahead (регресс-защита)."""
    from PySide6.QtCore import Qt
    mw, ui_mod = _fake_window({0: {"cloud_path": "/a.txt", "name": "a.txt"}})

    ui_mod.MainWindow._handle_table_key(mw, _key_event(Qt.Key_A, Qt.NoModifier, "a"))

    mw._select_all_files.assert_not_called()
    mw._select_by_typeahead.assert_called_once()
