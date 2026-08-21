# tests/test_tree_model_db.py
"""Тесты DB-first наполнения FolderTreeModel и точных стрелок раскрытия."""
import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def model(qapp, populated_db):
    from ui_tree_model import FolderTreeModel
    # Девиация от плана: Database(":memory:") создаёт ДВА независимых
    # in-memory соединения (_conn и _read_conn), поэтому batch-запрос
    # статусов через _read_conn не видел таблицу files. Сводим соединения
    # к одному только для тестов (в проде путь файловый — там всё ок).
    # Правильный фикс — в conftest.in_memory_db, вынесен отдельным пунктом.
    populated_db._read_conn = populated_db._conn
    # Корень «Яндекс Диск» существует сразу после __init__ (visible_root);
    # НЕ вызываем populate_children — он пометил бы корень loaded=True
    return FolderTreeModel(None, populated_db)


def test_populate_from_db_exact_arrows(model):
    folders, subs = model._db.get_dir_tree_levels("/")
    model.populate_children_from_db("/", folders, subs)

    # Три дочерних узла корня
    assert model.rowCount(model.index(0, 0)) == 3

    # Стрелки точные: у /Фото есть подпапки, у остальных нет
    photo_idx = model.index(2, 0, model.index(0, 0))   # sorted: Документы, Музыка, Фото
    docs_idx = model.index(0, 0, model.index(0, 0))
    assert model.hasChildren(photo_idx) is True
    assert model.hasChildren(docs_idx) is False


def test_populate_from_db_sets_flag(model):
    folders, subs = model._db.get_dir_tree_levels("/")
    model.populate_children_from_db("/", folders, subs)
    item = model._find_item("/")
    assert item.db_loaded is True
    assert item.loaded is False  # API ещё не сверял


def test_populate_twice_is_noop(model):
    folders, subs = model._db.get_dir_tree_levels("/")
    model.populate_children_from_db("/", folders, subs)
    model.populate_children_from_db("/", folders, subs)  # второй вызов — no-op
    assert model.rowCount(model.index(0, 0)) == 3
