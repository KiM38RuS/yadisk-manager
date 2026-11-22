"""Инбокс PLANS: горизонтальный скролл должен меняться прямо во время
перетаскивания границы колонки (пока зажата ЛКМ), а не только после отпускания.

Причина: таблица была в режиме ScrollPerItem — диапазон скролбара считался
в колонках и пересчитывался с задержкой. ScrollPerPixel даёт точный диапазон
в пикселях на каждую эмиссию QHeaderView.sectionResized (которая приходит
во время drag).
"""

import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_horizontal_scroll_is_pixel_based_and_tracks_column_resize(qapp):
    from PySide6.QtWidgets import QAbstractItemView
    from ui_table_model import (
        FileTableModel, apply_table_header_defaults, COL_LOCATION,
    )
    from unittest.mock import MagicMock

    from PySide6.QtWidgets import QTableView
    view = QTableView()
    view.setModel(FileTableModel(MagicMock()))
    apply_table_header_defaults(view)
    view.setColumnHidden(COL_LOCATION, True)
    view.resize(600, 300)
    view.show()
    qapp.processEvents()

    assert view.horizontalScrollMode() == QAbstractItemView.ScrollPerPixel

    sb = view.horizontalScrollBar()
    header = view.horizontalHeader()
    before = sb.maximum()
    assert before > 0  # контент уже шире вьюпорта

    header.resizeSection(1, view.columnWidth(1) + 600)
    qapp.processEvents()
    assert sb.maximum() == before + 600

    header.resizeSection(1, 200)
    qapp.processEvents()
    assert sb.maximum() == 0  # скролл исчез, когда контент стал уже вьюпорта
