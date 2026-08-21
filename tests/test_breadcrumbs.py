# tests/test_breadcrumbs.py
"""Тесты адресной строки: нормализация путей, переполнение, сегменты."""
import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_normalize_basic():
    from ui_shared import normalize_cloud_path
    assert normalize_cloud_path("/Загрузки/Отчёты/") == "/Загрузки/Отчёты"
    assert normalize_cloud_path("/Загрузки/Отчёты") == "/Загрузки/Отчёты"  # канонический passthrough
    assert normalize_cloud_path("//a///b//") == "/a/b"
    assert normalize_cloud_path("") == "/"
    assert normalize_cloud_path("   ") == "/"
    assert normalize_cloud_path("relative/path") == "/relative/path"
    assert normalize_cloud_path("/") == "/"


def test_normalize_backslashes():
    """Вставка пути из Проводника Windows: '\\' → '/'."""
    from ui_shared import normalize_cloud_path
    assert normalize_cloud_path("Документы\\Фото\\2024") == "/Документы/Фото/2024"
    assert normalize_cloud_path("\\\\a\\b\\") == "/a/b"


def test_compute_overflow():
    from ui_breadcrumbs import compute_overflow
    # Всё помещается
    assert compute_overflow(300, 400, [100, 100, 100]) == 0
    # Нужно убрать один средний
    assert compute_overflow(350, 300, [100, 100, 100, 50]) == 1
    # Два сегмента не трогаем никогда
    assert compute_overflow(500, 50, [250, 250]) == 0


def test_split_segments(qapp):
    from ui_breadcrumbs import BreadcrumbBar
    segs = BreadcrumbBar._split_segments("/Загрузки/Отчёты")
    assert segs == [
        ("Яндекс Диск", "/"),
        ("Загрузки", "/Загрузки"),
        ("Отчёты", "/Загрузки/Отчёты"),
    ]


def test_set_path_builds_buttons(qapp):
    from ui_breadcrumbs import BreadcrumbBar
    bar = BreadcrumbBar()
    bar._crumbs.setFixedWidth(2000)  # исключаем переполнение (offscreen-ширина)
    bar.set_path("/Загрузки/Отчёты")
    # Кнопки: Яндекс Диск, Загрузки, Отчёты (+ разделители QLabel)
    from PySide6.QtWidgets import QToolButton
    btns = [w for w in bar._crumbs.findChildren(QToolButton)]
    labels = [b.text() for b in btns]
    assert labels == ["Яндекс Диск", "Загрузки", "Отчёты"]
    # Последняя кнопка жирная
    assert btns[-1].font().bold() is True


def test_navigate_signal_on_crumb_click(qapp):
    from ui_breadcrumbs import BreadcrumbBar
    bar = BreadcrumbBar()
    bar._crumbs.setFixedWidth(2000)  # исключаем переполнение
    bar.set_path("/Загрузки/Отчёты")
    got = []
    bar.navigate.connect(got.append)
    from PySide6.QtWidgets import QToolButton
    btns = bar._crumbs.findChildren(QToolButton)
    btns[1].click()  # «Загрузки»
    assert got == ["/Загрузки"]


def test_editor_roundtrip(qapp):
    from ui_breadcrumbs import BreadcrumbBar
    bar = BreadcrumbBar()
    bar.set_path("/A")
    got = []
    bar.navigate.connect(got.append)
    bar.show_editor()
    # isHidden(): явное скрытие; в offscreen isVisible() всегда False,
    # т.к. родительское окно не показано
    assert bar._editor.isHidden() is False   # редактор активен
    assert bar._crumbs.isHidden() is True    # крошки спрятаны
    bar._editor.setText("/B/")
    bar._on_edit_done()
    assert got == ["/B"]                     # нормализовано: хвостовой слэш снят
    assert bar._editor.isHidden() is True    # вернулись к крошкам
    assert bar._crumbs.isHidden() is False
