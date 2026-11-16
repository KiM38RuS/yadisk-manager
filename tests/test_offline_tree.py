# tests/test_offline_tree.py
"""Офлайн-дерево папок из SQLite (PLANS 3.7)."""
import os
from types import MethodType
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _offline_window(db):
    """MagicMock-окно с настоящей моделью дерева и настоящими методами офлайна."""
    import ui as ui_mod
    from ui_tree_model import FolderTreeModel

    mw = MagicMock()
    mw._db = db
    mw.tree_model = FolderTreeModel(None, db)
    mw.tree_view = MagicMock()
    # Методы, чью реальную логику проверяем, берём у настоящего MainWindow
    mw._populate_tree_from_db = MethodType(
        ui_mod.MainWindow._populate_tree_from_db, mw)
    mw._is_auth_error = MethodType(ui_mod.MainWindow._is_auth_error, mw)
    return mw


def test_status_override_sets_unknown(qapp, populated_db):
    from ui_tree_model import FolderTreeModel
    model = FolderTreeModel(None, populated_db)
    folders, subs = populated_db.get_dir_tree_levels("/")

    model.populate_children_from_db("/", folders, subs, status_override="unknown")

    # Статус хранится на узле (data() отдаёт его через иконку, а не через роль)
    assert model._find_item("/Документы").status == "unknown"
    assert model._find_item("/Фото").status == "unknown"


def test_populate_tree_from_db_marks_unknown(qapp, populated_db):
    import ui as ui_mod
    from ui_tree_model import FolderTreeModel

    mw = MagicMock()
    mw._db = populated_db
    mw.tree_model = FolderTreeModel(None, populated_db)

    ui_mod.MainWindow._populate_tree_from_db(mw, "/")

    root = mw.tree_model.index(0, 0)
    assert mw.tree_model.rowCount(root) == 3
    assert mw.tree_model._find_item("/Музыка").status == "unknown"


def test_offline_startup_expands_tree_and_hides_file_loading(qapp, populated_db):
    """Старт без сети: корень раскрыт, «Загрузка файлов...» не висит."""
    import ui as ui_mod

    mw = _offline_window(populated_db)
    mw._expand_tree_root = MagicMock()

    ui_mod.MainWindow._on_tree_loaded(mw, [], "network unreachable")

    # Дерево действительно наполнено из SQLite
    root = mw.tree_model.index(0, 0)
    assert mw.tree_model.rowCount(root) == 3
    assert mw.tree_model._find_item("/Документы") is not None
    # PLANS 3.7: офлайн-дерево не должно оставаться нераскрытым
    mw._expand_tree_root.assert_called_once()
    # Индикатор «Загрузка файлов...» не включается и не висит
    mw._hide_loading.assert_called_once()
    mw._show_loading.assert_not_called()
    # Таблица грузится только из SQLite, без полного обхода API
    mw._start_folder_load.assert_called_once_with(
        "/", use_api=False, after_all_files=False)


def test_offline_expand_does_not_query_db_for_loaded_node(qapp, populated_db):
    """Разворачивание уже наполненного из БД узла не дёргает SQLite повторно."""
    import ui as ui_mod

    mw = _offline_window(populated_db)
    mw._online = False
    # Корень уже наполнен из SQLite → db_loaded
    ui_mod.MainWindow._populate_tree_from_db(mw, "/")
    assert mw.tree_model._find_item("/").db_loaded
    mw._populate_tree_from_db = MagicMock()

    ui_mod.MainWindow._on_tree_item_expanded(mw, mw.tree_model.index(0, 0))
    mw._populate_tree_from_db.assert_not_called()

    # Ненаполненный узел по-прежнему берёт подпапки из SQLite
    item = mw.tree_model._find_item("/Документы")
    assert item is not None and not item.db_loaded
    ui_mod.MainWindow._on_tree_item_expanded(
        mw, mw.tree_model._index_of(item))
    mw._populate_tree_from_db.assert_called_once_with("/Документы")


def test_reconnect_refreshes_tree_only(qapp, populated_db):
    """Восстановление связи не сбрасывает таблицу на «/» и не трогает лоадеры."""
    import ui as ui_mod

    mw = _offline_window(populated_db)
    mw._online = False
    mw._update_app_icons = MagicMock()
    mw._fetch_folder_list = MagicMock()
    mw._start_bulk_sync = MagicMock()

    ui_mod.MainWindow._on_connectivity_result(mw, False)

    assert mw._online is True
    mw._fetch_folder_list.assert_called_once()
    path, callback = mw._fetch_folder_list.call_args.args
    assert path == "/"
    assert callback is mw._on_reconnect_tree_loaded
    mw._start_bulk_sync.assert_called_once()

    # Офлайн-корень из SQLite → reconnect мержит API-список поверх
    ui_mod.MainWindow._populate_tree_from_db(mw, "/")
    root = mw.tree_model.index(0, 0)
    assert mw.tree_model.rowCount(root) == 3

    mw._expand_tree_root = MagicMock()
    mw._hide_tree_loading = MagicMock()
    mw._start_folder_load = MagicMock()
    mw._show_table_loading = MagicMock()
    mw._show_loading = MagicMock()

    api_items = [
        {"path": "/Документы", "name": "Документы", "type": "dir"},
        {"path": "/Фото", "name": "Фото", "type": "dir"},
        {"path": "/Музыка", "name": "Музыка", "type": "dir"},
        {"path": "/Новая", "name": "Новая", "type": "dir"},
    ]
    ui_mod.MainWindow._on_reconnect_tree_loaded(mw, api_items, None)

    assert mw.tree_model.rowCount(root) == 4
    assert mw.tree_model._find_item("/Новая") is not None
    mw._expand_tree_root.assert_called_once()
    mw._hide_tree_loading.assert_called_once()
    mw._start_folder_load.assert_not_called()
    mw._show_table_loading.assert_not_called()
    mw._show_loading.assert_not_called()


def test_reconnect_tree_error_keeps_db_content(qapp, populated_db):
    """Ошибка при обновлении дерева после reconnect не ломает офлайн-дерево."""
    import ui as ui_mod

    mw = _offline_window(populated_db)
    ui_mod.MainWindow._populate_tree_from_db(mw, "/")
    mw._expand_tree_root = MagicMock()
    mw._hide_tree_loading = MagicMock()

    ui_mod.MainWindow._on_reconnect_tree_loaded(mw, [], "boom")

    root = mw.tree_model.index(0, 0)
    assert mw.tree_model.rowCount(root) == 3
    mw._expand_tree_root.assert_not_called()
    mw._hide_tree_loading.assert_not_called()
