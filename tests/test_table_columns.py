"""PLANS 2.11–2.13 — колонки таблицы: ширины, порядок, разделители.

* 2.12: столбец «Размер» был заблокирован (ResizeToContents — пользователь
  не мог менять ширину), у последнего столбца не было правой границы.
* 2.11: ширины колонок не сохранялись между запусками.
* 2.13: колонки нельзя было перетаскивать.
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


def _view():
    from PySide6.QtWidgets import QTableView
    from ui_table_model import FileTableModel, apply_table_header_defaults

    view = QTableView()
    view.setModel(FileTableModel(MagicMock()))
    apply_table_header_defaults(view)
    return view


def test_size_and_type_columns_are_user_resizable(qapp):
    from PySide6.QtWidgets import QHeaderView
    view = _view()
    header = view.horizontalHeader()
    header.setStretchLastSection(False)
    # «Размер» и «Тип» — Interactive: пользователь может тянуть разделитель
    assert header.sectionResizeMode(2) == QHeaderView.Interactive
    assert header.sectionResizeMode(3) == QHeaderView.Interactive
    # «Статус» — узкая фиксированная иконка
    assert header.sectionResizeMode(0) == QHeaderView.Fixed


def test_columns_are_movable(qapp):
    """PLANS 2.13: колонки можно перетаскивать."""
    view = _view()
    assert view.horizontalHeader().sectionsMovable() is True


def test_header_state_roundtrip(qapp):
    """PLANS 2.11: ширины и порядок колонок переживают сохранение/восстановление."""
    from ui_table_model import save_header_state, restore_header_state

    view = _view()
    header = view.horizontalHeader()
    view.setColumnWidth(1, 555)
    header.moveSection(0, 3)          # перетащили «Статус» правее
    moved_index = header.visualIndex(0)
    saved = save_header_state(header)
    assert saved

    other = _view()
    assert restore_header_state(other.horizontalHeader(), saved) is True
    assert other.columnWidth(1) == 555
    assert other.horizontalHeader().visualIndex(0) == moved_index


def test_restore_header_state_rejects_empty_and_garbage(qapp):
    from ui_table_model import restore_header_state
    header = _view().horizontalHeader()
    assert restore_header_state(header, "") is False
    assert restore_header_state(header, "не-base64!!!") is False


def test_header_state_persisted_in_config(qapp, monkeypatch, tmp_path):
    """db-слой: состояние шапки хранится в config.json."""
    import db
    monkeypatch.setattr(db, "CONFIG_PATH", str(tmp_path / "config.json"))
    assert db.get_table_header_state() == ""
    db.set_table_header_state("AAA=")
    assert db.get_table_header_state() == "AAA="
    db.set_table_header_state("")
    assert db.get_table_header_state() == ""
