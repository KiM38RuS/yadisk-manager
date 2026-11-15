"""Тесты: sync_children_from_api возвращает локально скачанные файлы,
исчезнувшие из облака (их записи в БД не удаляются).
"""


def _item(path, md5="m"):
    return {"path": path, "name": path.rsplit("/", 1)[-1], "type": "file",
            "size": 1, "modified": "", "md5": md5}


def test_vanished_downloaded_file_is_returned_and_kept(in_memory_db, tmp_path):
    db = in_memory_db
    db.upsert_file("/photos/1.jpg", "1.jpg", "file", size=1, md5="old")
    db.set_downloaded("/photos/1.jpg", str(tmp_path / "1.jpg"), last_sync_md5="old")

    vanished = db.sync_children_from_api("/photos", [])

    assert [v["cloud_path"] for v in vanished] == ["/photos/1.jpg"]
    assert vanished[0]["local_path"] == str(tmp_path / "1.jpg")
    assert db.get_file("/photos/1.jpg") is not None      # запись не удалена


def test_vanished_cloud_only_is_pruned(in_memory_db):
    db = in_memory_db
    db.upsert_file("/photos/2.jpg", "2.jpg", "file", size=1, md5="x")

    vanished = db.sync_children_from_api("/photos", [])

    assert vanished == []
    assert db.get_file("/photos/2.jpg") is None


def test_vanished_syncing_is_not_returned(in_memory_db, tmp_path):
    db = in_memory_db
    db.set_syncing("/photos/3.jpg", str(tmp_path / "3.jpg"))

    vanished = db.sync_children_from_api("/photos", [])

    assert vanished == []
    assert db.get_file("/photos/3.jpg") is not None


def test_present_downloaded_file_is_not_vanished(in_memory_db, tmp_path):
    db = in_memory_db
    db.upsert_file("/photos/1.jpg", "1.jpg", "file", size=1, md5="m")
    db.set_downloaded("/photos/1.jpg", str(tmp_path / "1.jpg"), last_sync_md5="m")

    vanished = db.sync_children_from_api("/photos", [_item("/photos/1.jpg")])

    assert vanished == []
