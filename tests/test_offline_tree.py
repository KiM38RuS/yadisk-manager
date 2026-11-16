# tests/test_offline_tree.py
"""Офлайн-дерево папок из SQLite (PLANS 3.7)."""
import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


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
