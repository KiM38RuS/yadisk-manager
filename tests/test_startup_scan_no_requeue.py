# tests/test_startup_scan_no_requeue.py
import hashlib
import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_unchanged_tree_is_not_requeued_for_upload(qapp, in_memory_db, mock_api,
                                                   tmp_path, monkeypatch):
    """PLANS 2.3: при повторном запуске скачанные файлы не должны попадать
    в fixed_paths (очередь upload), если локальный MD5 совпадает с облачным."""
    import ui_threads

    content = b"stable"
    md5 = hashlib.md5(content).hexdigest()
    lp = tmp_path / "stable.txt"
    lp.write_bytes(content)
    items = [{"path": "/stable.txt", "name": "stable.txt", "type": "file",
              "size": len(content), "modified": "", "md5": md5}]
    mock_api.set_cloud_files(items)
    in_memory_db.upsert_files_batch(items)
    in_memory_db.set_downloaded("/stable.txt", str(lp), last_sync_md5=md5)

    monkeypatch.setattr(ui_threads, "_local_path", lambda cp: str(lp))
    t = ui_threads.FolderLoadThread(mock_api, in_memory_db, "/", use_api=True)
    got = []
    t.finished.connect(lambda *a: got.append(a))
    t.run()

    _, items_out, fixed_paths, reverse_checks, error, vanished = got[0]
    assert error == ""
    assert fixed_paths == []
    assert reverse_checks == []
    assert vanished == []
    assert items_out[0]["status"] == "downloaded"
