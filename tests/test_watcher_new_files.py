"""Tests for MainWindow._handle_file_created — detecting new files in the cache.

A file created outside the app (Explorer, another program) may arrive with
backslashes and different case.  The cache-prefix check must normalize both
sides, otherwise the watcher drops the event as "outside cache".

Covers PLANS 3.5.
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


@pytest.mark.skipif(os.name != "nt", reason="Windows-специфичная нормализация регистра")
def test_created_file_with_backslashes_and_case(qapp, tmp_path, monkeypatch):
    """Файл из Проводника: путь с '\\' и другим регистром диска должен
    распознаваться как файл внутри кеша и уходить в _register_new_file."""
    import ui as ui_mod

    mw = MagicMock()
    mw._db.get_by_local_path.return_value = None
    monkeypatch.setattr(ui_mod, "_cache_dir", lambda: str(tmp_path))

    local = str(tmp_path).upper().replace("/", "\\") + "\\New Folder\\a.txt"

    ui_mod.MainWindow._handle_file_created(mw, local)

    mw._register_new_file.assert_called_once()
    _, cloud_path = mw._register_new_file.call_args.args
    assert cloud_path == "/New Folder/a.txt"


@pytest.mark.skipif(os.name != "nt", reason="Windows-специфичная нормализация регистра")
def test_created_file_outside_cache_is_ignored(qapp, tmp_path, monkeypatch):
    """Файл вне кеша (соседняя папка) не должен регистрироваться."""
    import ui as ui_mod

    mw = MagicMock()
    mw._db.get_by_local_path.return_value = None
    monkeypatch.setattr(ui_mod, "_cache_dir", lambda: str(tmp_path))

    outside = str(tmp_path.parent / "outside.txt")

    ui_mod.MainWindow._handle_file_created(mw, outside)

    mw._register_new_file.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows-специфичная нормализация регистра")
def test_created_file_with_cache_dir_as_string_prefix_is_ignored(
        qapp, tmp_path, monkeypatch):
    """Путь, у которого кеш — только строковый префикс (`<cache>_evil`),
    не лежит в кеше и не должен регистрироваться."""
    import ui as ui_mod

    mw = MagicMock()
    mw._db.get_by_local_path.return_value = None
    monkeypatch.setattr(ui_mod, "_cache_dir", lambda: str(tmp_path))

    evil = str(tmp_path) + "_evil\\a.txt"

    ui_mod.MainWindow._handle_file_created(mw, evil)

    mw._register_new_file.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows-специфичная нормализация регистра")
def test_created_file_when_cache_is_drive_root(qapp, tmp_path, monkeypatch):
    """Кеш на корне диска (`C:\\`) — дочерний файл должен регистрироваться.

    `normpath("C:\\")` сохраняет хвостовой разделитель, поэтому наивное
    `cache_root + os.sep` даёт `c:\\\\` и отбрасывает все файлы диска.
    """
    import ui as ui_mod

    mw = MagicMock()
    mw._db.get_by_local_path.return_value = None
    drive_root = tmp_path.drive + os.sep
    monkeypatch.setattr(ui_mod, "_cache_dir", lambda: drive_root)

    local = tmp_path.drive + "\\somewhere\\a.txt"

    ui_mod.MainWindow._handle_file_created(mw, local)

    mw._register_new_file.assert_called_once()
    _, cloud_path = mw._register_new_file.call_args.args
    assert cloud_path == "/somewhere/a.txt"


@pytest.mark.skipif(os.name != "nt", reason="Windows-специфичная нормализация регистра")
def test_created_file_with_dotdot_segments(qapp, tmp_path, monkeypatch):
    """Путь с `..` должен нормализоваться в чистый облачный путь."""
    import ui as ui_mod

    mw = MagicMock()
    mw._db.get_by_local_path.return_value = None
    monkeypatch.setattr(ui_mod, "_cache_dir", lambda: str(tmp_path))

    local = (str(tmp_path) + "\\..\\" + tmp_path.name
             + "\\New Folder\\a.txt")

    ui_mod.MainWindow._handle_file_created(mw, local)

    mw._register_new_file.assert_called_once()
    _, cloud_path = mw._register_new_file.call_args.args
    assert cloud_path == "/New Folder/a.txt"


@pytest.mark.skipif(os.name != "nt", reason="Windows-специфичная нормализация регистра")
def test_cache_root_itself_is_not_registered(qapp, tmp_path, monkeypatch):
    """Сам корень кеша — не файл: `relpath` дал бы "." → "/.", это мусорная
    syncing-запись для каталога."""
    import ui as ui_mod

    mw = MagicMock()
    mw._db.get_by_local_path.return_value = None
    monkeypatch.setattr(ui_mod, "_cache_dir", lambda: str(tmp_path))

    ui_mod.MainWindow._handle_file_created(mw, str(tmp_path))

    mw._register_new_file.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows-специфичная нормализация регистра")
def test_created_file_plain_path_registers(qapp, tmp_path, monkeypatch):
    """Обычный путь в кеше (совпадающий регистр, `\\`) — регрессия common-path."""
    import ui as ui_mod

    mw = MagicMock()
    mw._db.get_by_local_path.return_value = None
    monkeypatch.setattr(ui_mod, "_cache_dir", lambda: str(tmp_path))

    local = str(tmp_path) + "\\a.txt"

    ui_mod.MainWindow._handle_file_created(mw, local)

    mw._register_new_file.assert_called_once()
    _, cloud_path = mw._register_new_file.call_args.args
    assert cloud_path == "/a.txt"
