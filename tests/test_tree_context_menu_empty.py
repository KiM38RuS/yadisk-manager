# tests/test_tree_context_menu_empty.py
"""ПКМ по пустому месту дерева (PLANS 1.3) + таргетинг текущей папки."""
import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _menu_no_parent(monkeypatch):
    """QMenu(MagicMock) не принимается PySide6 — фабрика без родителя."""
    import ui as ui_mod
    from PySide6.QtWidgets import QMenu as RealQMenu
    monkeypatch.setattr(ui_mod, "QMenu", lambda parent=None: RealQMenu())


def _menu_labels(menu):
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def _save_action(menu):
    return next(a for a in menu.actions()
                if a.text() == "Сохранить на компьютере")


def test_empty_tree_area_has_context_menu(qapp):
    import ui as ui_mod
    from PySide6.QtCore import QModelIndex

    mw = MagicMock()
    mw.tree_view.indexAt.return_value = QModelIndex()
    mw._current_path = "/"
    mw._clipboard_buffer = []
    mw._db.get_folder_aggregate_status.return_value = "cloud_only"

    menu = ui_mod.MainWindow._build_tree_empty_menu(mw)
    labels = _menu_labels(menu)

    assert "Сохранить на компьютере" in labels
    assert "Новая папка" in labels
    assert "Вставить" not in labels   # буфер пуст
    assert "Обновить" in labels
    # Точный порядок PLANS 1.3 (сепаратор между «Вставить» и «Обновить»)
    assert labels == ["Сохранить на компьютере", "Новая папка", "Обновить"]
    assert any(a.isSeparator() for a in menu.actions())


def test_empty_tree_area_inserts_paste_when_buffer_filled(qapp):
    import ui as ui_mod

    mw = MagicMock()
    mw._current_path = "/"
    mw._clipboard_buffer = ["/a.txt"]
    mw._db.get_folder_aggregate_status.return_value = "cloud_only"

    menu = ui_mod.MainWindow._build_tree_empty_menu(mw)
    labels = _menu_labels(menu)

    assert "Вставить" in labels
    assert labels == ["Сохранить на компьютере", "Новая папка",
                      "Вставить", "Обновить"]


def test_root_guard_skips_aggregate_and_disables_save(qapp):
    import ui as ui_mod

    # Корень: агрегат не запрашиваем, «Сохранить» недоступен
    root = MagicMock()
    root._current_path = "/"
    root._clipboard_buffer = []
    root._db.get_folder_aggregate_status.return_value = "cloud_only"

    menu = ui_mod.MainWindow._build_tree_empty_menu(root)
    root._db.get_folder_aggregate_status.assert_not_called()
    assert not _save_action(menu).isEnabled()

    # Подпапка: агрегат запрашивается, fully-downloaded -> недоступен
    sub = MagicMock()
    sub._current_path = "/Photos"
    sub._clipboard_buffer = []
    sub._db.get_folder_aggregate_status.return_value = "downloaded"

    menu = ui_mod.MainWindow._build_tree_empty_menu(sub)
    sub._db.get_folder_aggregate_status.assert_called_once_with("/Photos")
    assert not _save_action(menu).isEnabled()

    # Подпапка cloud_only -> доступен
    sub2 = MagicMock()
    sub2._current_path = "/Photos"
    sub2._clipboard_buffer = []
    sub2._db.get_folder_aggregate_status.return_value = "cloud_only"

    menu = ui_mod.MainWindow._build_tree_empty_menu(sub2)
    assert _save_action(menu).isEnabled()


def test_invalid_index_opens_empty_menu(qapp):
    """Реальный _on_tree_context_menu не должен делать early return."""
    import ui as ui_mod
    from PySide6.QtCore import QModelIndex, QPoint

    mw = MagicMock()
    mw.tree_view.indexAt.return_value = QModelIndex()
    global_pos = QPoint(11, 22)
    mw.tree_view.viewport.return_value.mapToGlobal.return_value = global_pos
    mock_menu = MagicMock()
    mw._build_tree_empty_menu = MagicMock(return_value=mock_menu)

    ui_mod.MainWindow._on_tree_context_menu(mw, QPoint(5, 7))

    mw._build_tree_empty_menu.assert_called_once_with()
    mock_menu.exec.assert_called_once_with(global_pos)


def test_empty_menu_save_action_binds_current_folder_helper(qapp):
    """«Сохранить на компьютере» из пустой области привязан к helper'у
    текущей папки, а не к selection-based _save_to_computer (ruling A)."""
    import ui as ui_mod

    mw = MagicMock()
    mw._current_path = "/Photos"
    mw._clipboard_buffer = []
    mw._db.get_folder_aggregate_status.return_value = "cloud_only"

    menu = ui_mod.MainWindow._build_tree_empty_menu(mw)
    action = _save_action(menu)
    assert action.isEnabled()

    # QAction.trigger() эмитит triggered(bool); PySide6 обрезает аргумент
    # для обработчика без параметров (и MagicMock его принимает).
    action.trigger()

    mw._save_current_folder_to_computer.assert_called_once()
    mw._save_to_computer.assert_not_called()


def test_save_current_folder_targets_current_path(qapp):
    import ui as ui_mod

    sub = MagicMock()
    sub._current_path = "/Photos"
    ui_mod.MainWindow._save_current_folder_to_computer(sub)
    sub._toggle_local_copy.assert_called_once_with(
        download_mode=True, folder_paths=["/Photos"])

    root = MagicMock()
    root._current_path = "/"
    ui_mod.MainWindow._save_current_folder_to_computer(root)
    root._toggle_local_copy.assert_not_called()

    empty = MagicMock()
    empty._current_path = ""
    ui_mod.MainWindow._save_current_folder_to_computer(empty)
    empty._toggle_local_copy.assert_not_called()
