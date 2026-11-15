"""Тесты: поиск облачной записи по MD5 и полный список локальных
файлов, отсутствующих в облаке (для сопоставления перемещений и очистки).
"""


def test_find_file_by_md5_prefers_cloud_row(in_memory_db, tmp_path):
    db = in_memory_db
    db.upsert_file("/photos/old.jpg", "old.jpg", "file", size=1, md5="same")
    db.set_downloaded("/photos/old.jpg", str(tmp_path / "old.jpg"), last_sync_md5="same")
    db.upsert_file("/photos/new.jpg", "new.jpg", "file", size=1, md5="same")

    found = db.find_file_by_md5("same", parent_path="/photos")

    assert found["cloud_path"] == "/photos/new.jpg"


def test_find_file_by_md5_respects_parent_and_exclude(in_memory_db):
    db = in_memory_db
    db.upsert_file("/a/x.jpg", "x.jpg", "file", size=1, md5="m")
    db.upsert_file("/b/x.jpg", "x.jpg", "file", size=1, md5="m")

    assert db.find_file_by_md5("m", parent_path="/a")["cloud_path"] == "/a/x.jpg"
    assert db.find_file_by_md5("m", parent_path="/a", exclude_path="/a/x.jpg") is None


def test_get_local_files_not_in(in_memory_db, tmp_path):
    db = in_memory_db
    db.upsert_file("/keep.txt", "keep.txt", "file", size=1, md5="1")
    db.set_downloaded("/keep.txt", str(tmp_path / "keep.txt"), last_sync_md5="1")
    db.upsert_file("/gone.txt", "gone.txt", "file", size=1, md5="2")
    db.set_downloaded("/gone.txt", str(tmp_path / "gone.txt"), last_sync_md5="2")
    db.upsert_file("/cloud.txt", "cloud.txt", "file", size=1, md5="3")

    rows = db.get_local_files_not_in({"/keep.txt", "/cloud.txt"})

    assert [r["cloud_path"] for r in rows] == ["/gone.txt"]
