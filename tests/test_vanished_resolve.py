import hashlib
import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _seed(db, tmp_path, content=b"same"):
    lp = tmp_path / "old.jpg"
    lp.write_bytes(content)
    md5 = hashlib.md5(content).hexdigest()
    db.upsert_file("/photos/old.jpg", "old.jpg", "file", size=len(content), md5=md5)
    db.set_downloaded("/photos/old.jpg", str(lp), last_sync_md5=md5)
    return lp, md5


def _run(db, vanished):
    from ui_threads import VanishedResolveThread
    t = VanishedResolveThread(None, db, vanished)
    out = []
    t.finished.connect(out.extend)
    t.run()
    return out


def _vanished(cp):
    return {"cloud_path": cp}


def test_unchanged_local_file_is_deleted(qapp, in_memory_db, tmp_path):
    lp, md5 = _seed(in_memory_db, tmp_path)
    out = _run(in_memory_db, [{"cloud_path": "/photos/old.jpg"}])
    assert out == [{"action": "delete", "cloud_path": "/photos/old.jpg",
                    "local_path": str(lp)}]


def test_missing_local_copy_drops_row(qapp, in_memory_db, tmp_path):
    lp, md5 = _seed(in_memory_db, tmp_path)
    os.remove(lp)
    out = _run(in_memory_db, [{"cloud_path": "/photos/old.jpg"}])
    assert out[0]["action"] == "drop"


def test_changed_local_file_conflicts(qapp, in_memory_db, tmp_path):
    lp, md5 = _seed(in_memory_db, tmp_path)
    lp.write_bytes(b"local edit")
    out = _run(in_memory_db, [{"cloud_path": "/photos/old.jpg"}])
    assert out[0]["action"] == "conflict"


def test_new_cloud_file_with_same_md5_becomes_move(qapp, in_memory_db, tmp_path):
    lp, md5 = _seed(in_memory_db, tmp_path)
    in_memory_db.upsert_file("/photos/new.jpg", "new.jpg", "file",
                             size=4, md5=md5)   # API уже переименовал файл
    out = _run(in_memory_db, [{"cloud_path": "/photos/old.jpg"}])
    assert out[0]["action"] == "move"
    assert out[0]["new_cloud_path"] == "/photos/new.jpg"


def test_modified_status_conflicts_even_without_last_sync(qapp, in_memory_db,
                                                          tmp_path):
    """Решение «оставить локальную версию» не должно откатываться в delete."""
    lp, md5 = _seed(in_memory_db, tmp_path)
    in_memory_db.set_status("/photos/old.jpg", "modified")
    in_memory_db.update_last_sync_md5("/photos/old.jpg", "")
    out = _run(in_memory_db, [{"cloud_path": "/photos/old.jpg",
                               "local_path": str(lp),
                               "last_sync_md5": "", "status": "modified"}])
    assert out[0]["action"] == "conflict"
