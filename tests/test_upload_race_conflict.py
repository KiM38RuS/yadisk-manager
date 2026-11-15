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
    mw._upload_stamps = {}
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


def test_upload_of_unchanged_file_is_not_dirty(qapp, in_memory_db, tmp_path):
    import hashlib

    db = in_memory_db
    db.upsert_file("/a.txt", "a.txt", "file", size=1, md5="")
    db.set_downloaded("/a.txt", str(tmp_path / "a.txt"), last_sync_md5="old")
    local = tmp_path / "a.txt"
    data = b"sent bytes"
    local.write_bytes(data)
    sent_md5 = hashlib.md5(data).hexdigest()   # локальный MD5 == отправленному

    mw, ui_mod = _window(db, tmp_path)
    mw._download_results.put(("/a.txt", str(local), sent_md5, "upload", False))

    with patch.object(ui_mod.MainWindow, "_refresh_folder_view"), \
         patch.object(ui_mod.MainWindow, "_update_tree_status"):
        ui_mod.MainWindow._process_result_queue(mw)

    rec = db.get_file("/a.txt")
    assert rec["last_sync_md5"] == sent_md5
    assert "/a.txt" not in mw._dirty_uploads    # нет более новой локальной версии
    assert "/a.txt" in mw._recently_uploaded


def test_upload_with_unchanged_stat_skips_hashing(qapp, in_memory_db, tmp_path):
    db = in_memory_db
    db.upsert_file("/a.txt", "a.txt", "file", size=1, md5="")
    db.set_downloaded("/a.txt", str(tmp_path / "a.txt"), last_sync_md5="old")
    local = tmp_path / "a.txt"
    local.write_bytes(b"some bytes")

    mw, ui_mod = _window(db, tmp_path)
    st = os.stat(local)
    mw._upload_stamps["/a.txt"] = (st.st_size, st.st_mtime_ns)
    mw._download_results.put(("/a.txt", str(local), "sent-md5", "upload", False))

    with patch.object(ui_mod.MainWindow, "_refresh_folder_view"), \
         patch.object(ui_mod.MainWindow, "_update_tree_status"), \
         patch.object(ui_mod, "_md5_file") as md5_file:
        ui_mod.MainWindow._process_result_queue(mw)

    assert not md5_file.called                # stat совпал — хеш не нужен
    assert "/a.txt" not in mw._dirty_uploads
    assert "/a.txt" not in mw._upload_stamps  # снимок израсходован


def test_created_while_syncing_marks_dirty_not_registered(qapp, in_memory_db, tmp_path):
    db = in_memory_db
    db.upsert_file("/a.txt", "a.txt", "file", size=1, md5="m")
    db.set_downloaded("/a.txt", str(tmp_path / "a.txt"), last_sync_md5="m")
    (tmp_path / "a.txt").write_bytes(b"changed during upload")
    mw, ui_mod = _window(db, tmp_path)
    mw._syncing = {"/a.txt"}

    with patch.object(ui_mod.MainWindow, "_register_new_file") as register:
        ui_mod.MainWindow._handle_file_created(mw, str(tmp_path / "a.txt"))

    assert "/a.txt" in mw._dirty_uploads
    assert not register.called
    assert "/a.txt" not in mw._pend_upload
