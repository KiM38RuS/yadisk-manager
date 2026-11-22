"""Regression: файл, помеченный syncing watcher'ом, не должен застревать,
если MetaFetch выяснил, что облачная и локальная версии уже совпадают.

Реальный сценарий (19 записей в БД 2026-11-21): watcher ловит modified
от Яндекс.Диска/другого процесса на неизменённом файле →
``_handle_file_modified`` ставит status='syncing' → MetaFetch видит
cloud_md5 == local_md5, upload пропускается → статус остаётся 'syncing'
навсегда (до перезапуска приложения).
"""

import hashlib
import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _meta_result(md5: str, size: int) -> dict:
    """Полный ответ _MetaFetchThread (все поля, которые читает обработчик)."""
    return {
        "cloud_md5": md5,
        "cloud_modified": "2026-11-21T08:00:00+00:00",
        "cloud_size": size,
        "local_md5": md5,
    }


def _window(db):
    mw = MagicMock()
    mw._db = db
    mw._active_meta_fetches = 1
    mw._meta_fetch_queue = []
    mw._reverse_meta_queue = []
    mw._syncing = set()
    mw._current_path = "/"
    return mw


def test_meta_fetch_already_in_sync_restores_downloaded_status(
    qapp, in_memory_db, tmp_path
):
    db = in_memory_db
    local = tmp_path / "a.txt"
    data = b"unchanged content"
    local.write_bytes(data)
    md5 = hashlib.md5(data).hexdigest()

    db.upsert_file("/a.txt", "a.txt", "file", size=len(data), md5=md5)
    db.set_downloaded("/a.txt", str(local), last_sync_md5=md5)
    db.set_status("/a.txt", "syncing")   # как после watcher: modified без изменений
    assert db.get_file("/a.txt")["status"] == "syncing"

    mw = _window(db)
    import ui as ui_mod
    ui_mod.MainWindow._on_meta_fetched(
        mw, "/a.txt", str(local), md5, _meta_result(md5, len(data))
    )

    rec = db.get_file("/a.txt")
    assert rec["status"] == "downloaded"
    assert rec["last_sync_md5"] == md5
