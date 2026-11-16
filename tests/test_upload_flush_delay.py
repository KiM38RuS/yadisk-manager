"""Tests for the debounced upload flush (PLANS 3.6).

Files collapsing into a save series (editor autosave, unzip, Explorer copy)
must reach the cloud quickly, but without one upload burst per event.  The
2 s fixed timer is replaced by a 250 ms single-shot timer that is restarted
on every new event (coalescing).
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


class _Stub:
    _upload_flush_timer = None

    def _flush_pending_upload(self):
        pass


class _CountingStub:
    _upload_flush_timer = None

    def __init__(self):
        self.flush_calls = 0

    def _flush_pending_upload(self):
        self.flush_calls += 1


def test_flush_delay_is_short_and_single_shot(qapp):
    import ui as ui_mod

    stub = _Stub()
    ui_mod.MainWindow._schedule_upload_flush(stub)

    assert ui_mod.UPLOAD_FLUSH_DELAY_MS <= 500
    assert stub._upload_flush_timer.isSingleShot()
    assert stub._upload_flush_timer.interval() == ui_mod.UPLOAD_FLUSH_DELAY_MS


def test_repeated_schedules_coalesce_into_one_flush(qapp):
    """Серия событий = один flush: второй start() перезапускает таймер."""
    from PySide6.QtTest import QTest

    import ui as ui_mod

    stub = _CountingStub()
    ui_mod.MainWindow._schedule_upload_flush(stub)
    ui_mod.MainWindow._schedule_upload_flush(stub)

    QTest.qWait(ui_mod.UPLOAD_FLUSH_DELAY_MS + 150)
    assert stub.flush_calls == 1


def _window(db):
    import ui as ui_mod

    mw = MagicMock()
    mw._db = db
    mw._pend_upload = set()
    mw._syncing = set()
    mw._dirty_uploads = set()
    mw._recently_downloaded = set()
    mw._current_path = "/"
    return mw, ui_mod


def test_modified_uses_debounced_flush_not_two_seconds(
        qapp, in_memory_db, tmp_path):
    db = in_memory_db
    local = tmp_path / "a.txt"
    local.write_bytes(b"edited")
    db.upsert_file("/a.txt", "a.txt", "file", size=6, md5="m")
    db.set_downloaded("/a.txt", str(local), last_sync_md5="m")
    mw, ui_mod = _window(db)

    with patch.object(mw, "_schedule_upload_flush") as sched, \
         patch.object(ui_mod.QTimer, "singleShot") as shot, \
         patch.object(ui_mod.MainWindow, "_refresh_folder_view"), \
         patch.object(ui_mod.MainWindow, "_update_tree_status"):
        ui_mod.MainWindow._handle_file_modified(mw, str(local))

    assert "/a.txt" in mw._pend_upload
    sched.assert_called_once()
    assert all(call.args[0] != 2000 for call in shot.call_args_list)


def test_created_new_in_cache_file_uses_debounced_flush(
        qapp, tmp_path, monkeypatch):
    import ui as ui_mod

    mw = MagicMock()
    mw._db.get_by_local_path.return_value = None
    monkeypatch.setattr(ui_mod, "_cache_dir", lambda: str(tmp_path))
    local = str(tmp_path / "new.txt")
    Path(local).write_bytes(b"new")

    with patch.object(mw, "_schedule_upload_flush") as sched, \
         patch.object(ui_mod.QTimer, "singleShot") as shot:
        ui_mod.MainWindow._handle_file_created(mw, local)

    mw._register_new_file.assert_called_once()
    sched.assert_called_once()
    assert all(call.args[0] != 2000 for call in shot.call_args_list)


def test_no_two_second_flush_left_in_source():
    """Структурный guard: все фиксированные 2 s flush заменены."""
    source = (Path(__file__).resolve().parent.parent / "ui.py").read_text(
        encoding="utf-8")
    assert "singleShot(2000" not in source
