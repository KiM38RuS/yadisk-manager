"""PLANS 1.11 — «Копировать как путь» (облачный + локальный путь в буфер)."""
import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_single_item_has_cloud_and_local_path():
    from ui_shared import build_copy_paths_text
    item = {"cloud_path": "/Документы/отчёт.docx"}
    text = build_copy_paths_text([item], lambda cp: r"C:\cache\Документы\отчёт.docx")
    assert text == (
        "Облако: /Документы/отчёт.docx\n"
        "Компьютер: C:\\cache\\Документы\\отчёт.docx"
    )


def test_multiple_items_separated_by_blank_line():
    from ui_shared import build_copy_paths_text
    items = [{"cloud_path": "/a.txt"}, {"cloud_path": "/b.txt"}]
    text = build_copy_paths_text(items, lambda cp: "L:" + cp.replace("/", "\\"))
    assert text.splitlines() == [
        "Облако: /a.txt",
        "Компьютер: L:\\a.txt",
        "",
        "Облако: /b.txt",
        "Компьютер: L:\\b.txt",
    ]


def test_local_path_omitted_when_resolver_returns_empty():
    from ui_shared import build_copy_paths_text
    text = build_copy_paths_text([{"cloud_path": "/a.txt"}], lambda cp: "")
    assert text == "Облако: /a.txt"


def test_copy_paths_to_clipboard(qapp):
    """ПКМ → «Копировать как путь»: в буфере пути выделенных элементов."""
    from PySide6.QtWidgets import QApplication
    import ui as ui_mod

    QApplication.clipboard().setText("")
    items = {
        0: {"cloud_path": "/A/файл.txt", "is_parent_nav": False},
        1: {"cloud_path": "/A", "is_parent_nav": True},
    }
    mw = MagicMock()
    mw._get_item.side_effect = lambda idx: items.get(idx.row())
    mw.table_view.selectionModel().selectedRows.return_value = [
        MagicMock(row=(lambda r=r: r)) for r in items]

    ui_mod.MainWindow._copy_paths_to_clipboard(
        mw, local_path_fn=lambda cp: r"C:\cache" + cp.replace("/", "\\"))

    text = QApplication.clipboard().text()
    assert "Облако: /A/файл.txt" in text
    assert "Компьютер: C:\\cache\\A\\файл.txt" in text
    assert "Облако: /A\n" not in text      # «..» в буфер не попадает
    mw.statusBar().showMessage.assert_called_once()


def test_copy_paths_to_clipboard_no_selection_is_noop(qapp):
    from PySide6.QtWidgets import QApplication
    import ui as ui_mod

    QApplication.clipboard().setText("прежний")
    mw = MagicMock()
    mw.table_view.selectionModel().selectedRows.return_value = []

    ui_mod.MainWindow._copy_paths_to_clipboard(mw, False)  # checked=False от QAction

    assert QApplication.clipboard().text() == "прежний"


def test_copy_paths_to_clipboard_from_tree_path(qapp):
    """ПКМ по папке в дереве: копируется именно эта папка, не выделение таблицы."""
    from PySide6.QtWidgets import QApplication
    import ui as ui_mod

    QApplication.clipboard().setText("")
    mw = MagicMock()

    ui_mod.MainWindow._copy_paths_to_clipboard(
        mw, ["/Документы"], local_path_fn=lambda cp: "C:" + cp.replace("/", "\\"))

    assert QApplication.clipboard().text() == (
        "Облако: /Документы\nКомпьютер: C:\\Документы")
    mw.table_view.selectionModel.assert_not_called()
