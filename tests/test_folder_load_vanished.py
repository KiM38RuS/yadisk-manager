"""FolderLoadThread: исчезнувшие из облака файлы отдаются в UI (Task 6).

Навигация по папке должна не только синхронизировать БД, но и вернуть
список скачанных файлов, которых больше нет в облаке, чтобы UI запустил
их разрешение (offline / удаление / восстановление).
"""
import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_folder_load_emits_vanished(qapp, in_memory_db, mock_api, tmp_path, monkeypatch):
    import ui_threads

    lp = tmp_path / "1.jpg"
    lp.write_bytes(b"x")
    in_memory_db.upsert_file("/photos/1.jpg", "1.jpg", "file", size=1, md5="m")
    in_memory_db.set_downloaded("/photos/1.jpg", str(lp), last_sync_md5="m")
    mock_api.set_cloud_files([{"path": "/photos", "name": "photos", "type": "dir"}])

    monkeypatch.setattr(ui_threads, "_local_path", lambda cp: str(lp))
    t = ui_threads.FolderLoadThread(mock_api, in_memory_db, "/photos", use_api=True)
    got = []
    t.finished.connect(lambda *a: got.append(a))
    t.run()

    _, _, _, _, error, vanished = got[0]
    assert error == ""
    assert [v["cloud_path"] for v in vanished] == ["/photos/1.jpg"]
