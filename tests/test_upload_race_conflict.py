import os
from queue import Queue
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _window(db, tmp_path):
    import ui as ui_mod
    mw = MagicMock()
    mw._db = db
    mw._download_results = Queue()
    mw._syncing = set()
    mw._dirty_uploads = set()
    mw._recently_uploaded = set()
    mw._pend_upload = set()
    mw._active_uploads = 0
    mw._upload_queue = []
    mw._current_path = "/"
    mw._uploads_refreshed = True
    return mw, ui_mod


def test_upload_records_sent_md5_and_marks_dirty(qapp, in_memory_db, tmp_path):
    db = in_memory_db
    db.upsert_file("/a.txt", "a.txt", "file", size=1, md5="")
    db.set_downloaded("/a.txt", str(tmp_path / "a.txt"), last_sync_md5="old")
    local = tmp_path / "a.txt"
    local.write_bytes(b"newer version")   # локальный MD5 != отправленному

    mw, ui_mod = _window(db, tmp_path)
    mw._download_results.put(("/a.txt", str(local), "sent-md5", "upload", False))

    with patch.object(ui_mod.MainWindow, "_refresh_folder_view"), \
         patch.object(ui_mod.MainWindow, "_update_tree_status"):
        ui_mod.MainWindow._process_result_queue(mw)

    rec = db.get_file("/a.txt")
    assert rec["last_sync_md5"] == "sent-md5"   # MD5 отправленных байт
    assert "/a.txt" in mw._dirty_uploads        # есть более новая локальная версия
    assert "/a.txt" in mw._recently_uploaded


def test_dirty_upload_is_requeued_and_flushed(qapp, in_memory_db):
    mw, ui_mod = _window(in_memory_db, None)
    mw._dirty_uploads = {"/a.txt"}
    with patch.object(ui_mod.QTimer, "singleShot") as shot:
        ui_mod.MainWindow._requeue_dirty_uploads(mw)
    assert "/a.txt" in mw._pend_upload
    assert "/a.txt" not in mw._dirty_uploads
    assert shot.called


def test_modified_while_syncing_marks_dirty_not_lost(qapp, in_memory_db, tmp_path):
    db = in_memory_db
    db.upsert_file("/a.txt", "a.txt", "file", size=1, md5="m")
    db.set_downloaded("/a.txt", str(tmp_path / "a.txt"), last_sync_md5="m")
    mw, ui_mod = _window(db, tmp_path)
    mw._syncing = {"/a.txt"}

    ui_mod.MainWindow._handle_file_modified(mw, str(tmp_path / "a.txt"))

    assert "/a.txt" in mw._dirty_uploads
    assert "/a.txt" not in mw._pend_upload
