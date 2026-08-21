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
    assert normalize_cloud_path("//a///b//") == "/a/b"
    assert normalize_cloud_path("") == "/"
    assert normalize_cloud_path("   ") == "/"
    assert normalize_cloud_path("relative/path") == "/relative/path"
    assert normalize_cloud_path("/") == "/"
