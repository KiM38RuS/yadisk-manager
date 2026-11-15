import hashlib
import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _win(db, tmp_path):
    import ui as ui_mod
    mw = MagicMock()
    mw._db = db
    mw._recently_uploaded = set()
    mw._pend_upload = set()
    mw._current_path = "/photos"
    mw._local_path = lambda cp: str(tmp_path / cp.lstrip("/"))
    mw._vanished_thread_running = False
    mw._vanished_pending = []
    return mw, ui_mod


def test_delete_action_removes_file_and_row(qapp, in_memory_db, tmp_path):
    lp = tmp_path / "photos" / "old.jpg"
    lp.parent.mkdir()
    lp.write_bytes(b"x")
    in_memory_db.upsert_file("/photos/old.jpg", "old.jpg", "file", size=1, md5="m")
    in_memory_db.set_downloaded("/photos/old.jpg", str(lp), last_sync_md5="m")
    mw, ui_mod = _win(in_memory_db, tmp_path)

    with patch.object(ui_mod, "_local_path", lambda cp: str(tmp_path / cp.lstrip("/"))), \
         patch.object(ui_mod.MainWindow, "_refresh_folder_view"), \
         patch.object(ui_mod.MainWindow, "_update_tree_status"):
        ui_mod.MainWindow._on_vanished_resolved(mw, [
            {"action": "delete", "cloud_path": "/photos/old.jpg", "local_path": str(lp)}])

    assert not lp.exists()
    assert in_memory_db.get_file("/photos/old.jpg") is None


def test_move_action_relocates_local_copy(qapp, in_memory_db, tmp_path):
    old = tmp_path / "photos" / "old.jpg"
    old.parent.mkdir()
    old.write_bytes(b"x")
    in_memory_db.upsert_file("/photos/old.jpg", "old.jpg", "file", size=1, md5="m")
    in_memory_db.set_downloaded("/photos/old.jpg", str(old), last_sync_md5="m")
    mw, ui_mod = _win(in_memory_db, tmp_path)

    with patch.object(ui_mod, "_local_path", lambda cp: str(tmp_path / cp.lstrip("/"))), \
         patch.object(ui_mod.MainWindow, "_refresh_folder_view"), \
         patch.object(ui_mod.MainWindow, "_update_tree_status"):
        ui_mod.MainWindow._on_vanished_resolved(mw, [{
            "action": "move", "cloud_path": "/photos/old.jpg",
            "local_path": str(old), "new_cloud_path": "/photos/new.jpg",
            "local_md5": "m"}])

    assert not old.exists()
    assert (tmp_path / "photos" / "new.jpg").exists()
    assert in_memory_db.get_file("/photos/old.jpg") is None
    assert in_memory_db.get_file("/photos/new.jpg")["status"] == "downloaded"


def test_recently_uploaded_is_skipped(qapp, in_memory_db, tmp_path):
    mw, ui_mod = _win(in_memory_db, tmp_path)
    mw._recently_uploaded = {"/photos/old.jpg"}
    in_memory_db.upsert_file("/photos/old.jpg", "old.jpg", "file", size=1, md5="m")

    ui_mod.MainWindow._on_vanished_resolved(mw, [
        {"action": "delete", "cloud_path": "/photos/old.jpg", "local_path": "x"}])

    assert in_memory_db.get_file("/photos/old.jpg") is not None


def test_offscreen_conflict_keeps_local_and_requeues(qapp, in_memory_db, tmp_path):
    lp = tmp_path / "photos" / "old.jpg"
    lp.parent.mkdir()
    lp.write_bytes(b"x")
    in_memory_db.upsert_file("/photos/old.jpg", "old.jpg", "file", size=1, md5="m")
    in_memory_db.set_downloaded("/photos/old.jpg", str(lp), last_sync_md5="m")
    mw, ui_mod = _win(in_memory_db, tmp_path)

    with patch.object(ui_mod, "_local_path", lambda cp: str(tmp_path / cp.lstrip("/"))), \
         patch.object(ui_mod.QGuiApplication, "platformName", lambda: "offscreen"), \
         patch.object(ui_mod.QTimer, "singleShot"):
        ui_mod.MainWindow._on_vanished_resolved(mw, [{
            "action": "conflict", "cloud_path": "/photos/old.jpg",
            "local_path": str(lp), "local_md5": "local"}])

    assert lp.exists()
    assert "/photos/old.jpg" in mw._pend_upload


def test_keep_local_conflict_marks_row_modified(qapp, in_memory_db, tmp_path):
    lp = tmp_path / "photos" / "old.jpg"
    lp.parent.mkdir()
    lp.write_bytes(b"x")
    in_memory_db.upsert_file("/photos/old.jpg", "old.jpg", "file", size=1, md5="m")
    in_memory_db.set_downloaded("/photos/old.jpg", str(lp), last_sync_md5="m")
    mw, ui_mod = _win(in_memory_db, tmp_path)

    with patch.object(ui_mod, "_local_path", lambda cp: str(tmp_path / cp.lstrip("/"))), \
         patch.object(ui_mod.QGuiApplication, "platformName", lambda: "offscreen"), \
         patch.object(ui_mod.QTimer, "singleShot"):
        ui_mod.MainWindow._on_vanished_resolved(mw, [{
            "action": "conflict", "cloud_path": "/photos/old.jpg",
            "local_path": str(lp), "local_md5": "local"}])

    row = in_memory_db.get_file("/photos/old.jpg")
    assert row is not None
    assert row["status"] == "modified"


def test_move_does_not_overwrite_different_destination(qapp, in_memory_db,
                                                       tmp_path):
    old = tmp_path / "photos" / "old.jpg"
    new = tmp_path / "photos" / "new.jpg"
    old.parent.mkdir()
    old.write_bytes(b"local")
    new.write_bytes(b"other")
    in_memory_db.upsert_file("/photos/old.jpg", "old.jpg", "file", size=5, md5="m")
    in_memory_db.set_downloaded("/photos/old.jpg", str(old), last_sync_md5="m")
    mw, ui_mod = _win(in_memory_db, tmp_path)

    with patch.object(ui_mod, "_local_path", lambda cp: str(tmp_path / cp.lstrip("/"))), \
         patch.object(ui_mod.MainWindow, "_refresh_folder_view"), \
         patch.object(ui_mod.MainWindow, "_update_tree_status"):
        ui_mod.MainWindow._on_vanished_resolved(mw, [{
            "action": "move", "cloud_path": "/photos/old.jpg",
            "local_path": str(old), "new_cloud_path": "/photos/new.jpg",
            "local_md5": "m"}])

    assert old.read_bytes() == b"local"
    assert new.read_bytes() == b"other"
    assert in_memory_db.get_file("/photos/old.jpg") is not None
