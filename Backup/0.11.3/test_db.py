"""
Tests for db.py — SQLite database layer.

Focuses on:
  1. CRUD operations
  2. Status transitions (cloud_only → downloaded → syncing → downloaded)
  3. Folder status queries (is_folder_fully_synced, get_unsynced_children)
  4. get_children() correctness
"""

import os
import pytest


class TestDatabaseBasics:
    """Basic CRUD and status transitions."""

    def test_upsert_and_get(self, in_memory_db):
        db = in_memory_db
        db.upsert_file("/test.txt", "test.txt", "file", size=100,
                       modified="2025-01-01T00:00:00Z", md5="abc123")
        rec = db.get_file("/test.txt")
        assert rec is not None
        assert rec["name"] == "test.txt"
        assert rec["size"] == 100
        assert rec["status"] == "cloud_only"
        assert rec["md5"] == "abc123"

    def test_upsert_updates_existing(self, in_memory_db):
        db = in_memory_db
        db.upsert_file("/test.txt", "test.txt", "file", size=100,
                       md5="v1")
        db.upsert_file("/test.txt", "test.txt", "file", size=200,
                       md5="v2")
        rec = db.get_file("/test.txt")
        assert rec["size"] == 200
        assert rec["md5"] == "v2"

    def test_set_downloaded(self, in_memory_db):
        db = in_memory_db
        db.upsert_file("/doc.pdf", "doc.pdf", "file", size=500, md5="x")
        db.set_downloaded("/doc.pdf", "/cache/doc.pdf", last_sync_md5="x")
        rec = db.get_file("/doc.pdf")
        assert rec["status"] == "downloaded"
        assert rec["local_path"] == "/cache/doc.pdf"
        assert rec["last_sync_md5"] == "x"

    def test_set_cloud_only(self, in_memory_db):
        db = in_memory_db
        db.upsert_file("/f.txt", "f.txt", "file", size=10, md5="m")
        db.set_downloaded("/f.txt", "/cache/f.txt", last_sync_md5="m")
        db.set_cloud_only("/f.txt")
        rec = db.get_file("/f.txt")
        assert rec["status"] == "cloud_only"
        assert rec["local_path"] is None

    def test_status_transition_full_cycle(self, in_memory_db):
        """cloud_only → downloaded → syncing → downloaded"""
        db = in_memory_db
        db.upsert_file("/cycle.txt", "cycle.txt", "file", md5="v1")
        assert db.get_file("/cycle.txt")["status"] == "cloud_only"

        db.set_downloaded("/cycle.txt", "/cache/cycle.txt", last_sync_md5="v1")
        assert db.get_file("/cycle.txt")["status"] == "downloaded"

        db.set_status("/cycle.txt", "syncing")
        assert db.get_file("/cycle.txt")["status"] == "syncing"

        db.set_downloaded("/cycle.txt", "/cache/cycle.txt", last_sync_md5="v2")
        assert db.get_file("/cycle.txt")["status"] == "downloaded"

    def test_remove_file(self, in_memory_db):
        db = in_memory_db
        db.upsert_file("/gone.txt", "gone.txt", "file")
        assert db.file_exists("/gone.txt")
        db.remove_file("/gone.txt")
        assert not db.file_exists("/gone.txt")


class TestGetChildren:
    """Tests for get_children() including recursive behaviour."""

    def test_get_all_from_root(self, in_memory_db, sample_cloud_files):
        db = in_memory_db
        db.upsert_files_batch(sample_cloud_files)
        children = db.get_children("/")
        paths = {c["cloud_path"] for c in children}
        # All files (not directories) should appear via get_children
        assert "/Документы" in paths
        assert "/Фото" in paths
        assert "/Музыка" in paths
        assert "/readme.txt" in paths
        # Deeply nested files may or may not appear depending on
        # get_children implementation — recursive or not.
        # The current impl is recursive (LIKE prefix/%).

    def test_get_folder_children(self, in_memory_db, sample_cloud_files):
        db = in_memory_db
        db.upsert_files_batch(sample_cloud_files)
        children = db.get_children("/Фото")
        paths = {c["cloud_path"] for c in children}
        assert "/Фото/2024" in paths
        # get_children возвращает только прямых потомков (один уровень)
        assert "/Фото/2024/отпуск.jpg" not in paths
        assert "/Фото/2024/пляж.jpg" not in paths
        assert "/Документы" not in paths

    def test_empty_folder(self, in_memory_db):
        db = in_memory_db
        children = db.get_children("/")
        assert children == []

    def test_nonexistent_folder(self, in_memory_db):
        db = in_memory_db
        children = db.get_children("/no/such/path")
        assert children == []


class TestFolderStatus:
    """Tests for is_folder_fully_synced() — THE critical logic."""

    def test_folder_not_fully_synced(self, populated_db):
        """/Фото has one file downloaded, one cloud_only → NOT fully synced."""
        db = populated_db
        assert db.is_folder_fully_synced("/Фото") is False

    def test_folder_fully_synced(self, populated_db):
        """Mark ALL files under /Фото as downloaded → fully synced."""
        db = populated_db
        db.set_downloaded("/Фото/2024/пляж.jpg",
                          "/tmp/ydm-test/Фото/2024/пляж.jpg",
                          last_sync_md5="ccc333")
        assert db.is_folder_fully_synced("/Фото") is True

    def test_root_not_fully_synced(self, populated_db):
        """Root has files with various statuses → not fully synced."""
        db = populated_db
        assert db.is_folder_fully_synced("/") is False

    def test_nested_folder_fully_synced(self, populated_db):
        """/Фото/2024 has one downloaded, one cloud_only → NOT synced."""
        db = populated_db
        assert db.is_folder_fully_synced("/Фото/2024") is False

    def test_folder_with_only_subdirs(self, in_memory_db):
        """Folder with only subdirs (no files) → NOT fully synced."""
        db = in_memory_db
        db.upsert_file("/Пустая", "Пустая", "dir")
        db.upsert_file("/Пустая/ничего.txt", "ничего.txt", "file")
        # File not downloaded
        assert db.is_folder_fully_synced("/Пустая") is False

    def test_folder_no_files(self, in_memory_db):
        """Folder with zero files → NOT fully synced (vacuous=False)."""
        db = in_memory_db
        assert db.is_folder_fully_synced("/") is False

    def test_fully_synced_nested_via_set(self, populated_db):
        """Mark ALL files everywhere as downloaded → root is synced."""
        db = populated_db
        db.set_downloaded("/Фото/2024/пляж.jpg",
                          "/c/f.jpg", last_sync_md5="ccc333")
        db.set_downloaded("/Документы/отчёт.docx",
                          "/c/r.docx", last_sync_md5="aaa111")
        db.set_downloaded("/Музыка/трек.mp3",
                          "/c/t.mp3", last_sync_md5="ddd444")
        db.set_downloaded("/readme.txt",
                          "/c/r.txt", last_sync_md5="eee555")
        assert db.is_folder_fully_synced("/") is True
        assert db.is_folder_fully_synced("/Фото") is True
        assert db.is_folder_fully_synced("/Фото/2024") is True
        assert db.is_folder_fully_synced("/Документы") is True
        assert db.is_folder_fully_synced("/Музыка") is True


class TestUnsyncedChildren:
    """Tests for get_unsynced_children()."""

    def test_unsynced_in_folder(self, populated_db):
        """/Фото has one unsynced file."""
        db = populated_db
        unsynced = db.get_unsynced_children("/Фото")
        paths = {c["cloud_path"] for c in unsynced}
        assert "/Фото/2024/пляж.jpg" in paths
        assert "/Фото/2024/отпуск.jpg" not in paths  # already downloaded

    def test_unsynced_empty_when_all_downloaded(self, populated_db):
        """No unsynced files after marking everything downloaded."""
        db = populated_db
        for rec in db.get_all_files():
            if rec["type"] == "file":
                db.set_downloaded(
                    rec["cloud_path"],
                    f"/tmp/{rec['cloud_path'].lstrip('/')}",
                    last_sync_md5=rec.get("md5", ""),
                )
        unsynced = db.get_unsynced_children("/")
        assert unsynced == []


class TestBatchOperations:
    """Tests for bulk operations used in sync/startup."""

    def test_upsert_files_batch(self, in_memory_db):
        db = in_memory_db
        items = [
            {"path": "/a.txt", "name": "a.txt", "type": "file",
             "size": 10, "md5": "m1"},
            {"path": "/b.txt", "name": "b.txt", "type": "file",
             "size": 20, "md5": "m2"},
        ]
        db.upsert_files_batch(items)
        assert db.file_exists("/a.txt")
        assert db.file_exists("/b.txt")
        assert db.get_file("/a.txt")["size"] == 10

    def test_upsert_batch_updates_cloud_only(self, in_memory_db):
        """Batch upsert should not overwrite status."""
        db = in_memory_db
        db.upsert_file("/x.txt", "x.txt", "file", size=100, md5="v1")
        db.set_downloaded("/x.txt", "/c/x.txt", last_sync_md5="v1")

        # Batch upsert with same md5 — status should stay downloaded
        db.upsert_files_batch([
            {"path": "/x.txt", "name": "x.txt", "type": "file",
             "size": 100, "md5": "v1"},
        ])
        rec = db.get_file("/x.txt")
        assert rec["status"] == "downloaded"
        assert rec["local_path"] == "/c/x.txt"

    def test_get_by_status(self, in_memory_db, sample_cloud_files):
        db = in_memory_db
        db.upsert_files_batch(sample_cloud_files)
        db.set_downloaded("/readme.txt", "/c/r.txt", last_sync_md5="eee555")
        downloaded = db.get_by_status("downloaded")
        assert len(downloaded) == 1
        assert downloaded[0]["cloud_path"] == "/readme.txt"

    def test_get_by_local_path(self, in_memory_db):
        db = in_memory_db
        db.upsert_file("/test.txt", "test.txt", "file")
        db.set_downloaded("/test.txt", "/cache/test.txt", last_sync_md5="m")
        rec = db.get_by_local_path("/cache/test.txt")
        assert rec is not None
        assert rec["cloud_path"] == "/test.txt"
        assert db.get_by_local_path("/nonexistent") is None

    def test_get_changed_downloaded_files(self, in_memory_db):
        db = in_memory_db
        # Файл: cloud md5 == last_sync_md5 — не должен попасть в результат
        db.upsert_file("/ok.txt", "ok.txt", "file", md5="abc")
        db.set_downloaded("/ok.txt", "/c/ok.txt", last_sync_md5="abc")
        # Файл: cloud md5 != last_sync_md5 — должен попасть
        db.upsert_file("/changed.txt", "changed.txt", "file", md5="new_md5")
        db.set_downloaded("/changed.txt", "/c/changed.txt", last_sync_md5="old_md5")
        # Файл: cloud md5 пуст — не должен попасть
        db.upsert_file("/no_md5.txt", "no_md5.txt", "file", md5="")
        db.set_downloaded("/no_md5.txt", "/c/no_md5.txt", last_sync_md5="x")
        # Файл: last_sync_md5 пуст — не должен попасть
        db.upsert_file("/no_lsmd5.txt", "no_lsmd5.txt", "file", md5="x")
        db.set_downloaded("/no_lsmd5.txt", "/c/no_lsmd5.txt", last_sync_md5="")
        # Файл: cloud_only статус — не должен попасть
        db.upsert_file("/co.txt", "co.txt", "file", md5="co_md5")

        changed = db.get_changed_downloaded_files()
        assert len(changed) == 1
        assert changed[0]["cloud_path"] == "/changed.txt"


class TestConfigLock:
    """Регрессия: зависший держатель _config_lock не должен блокировать
    главный поток навсегда (фриз UI при закрытии настроек).

    Причина бага: AllFilesThread (фоновый) пишет offset в config.json
    (set_all_files_offset → save_config → _config_lock), а _svg_icon при
    отрисовке иконок вызывал get_theme → load_config → тот же lock.
    Нереентерабельный Lock без таймаута = вечный дедлок UI.
    """

    def test_load_config_lock_timeout(self):
        import time
        import db as db_mod

        lock = db_mod._config_lock
        lock.acquire()  # эмулируем «зависший» фоновый поток
        try:
            t0 = time.monotonic()
            cfg = db_mod.load_config()  # должна вернуться через ~3с, не вечно
            dt = time.monotonic() - t0
            assert cfg == {}  # при таймауте возвращается пустой конфиг
            assert 2.5 <= dt <= 5.0, f"ожидали ~3с таймаут, было {dt:.1f}с"
        finally:
            lock.release()

    def test_save_config_lock_timeout(self):
        import time
        import db as db_mod

        lock = db_mod._config_lock
        lock.acquire()
        try:
            t0 = time.monotonic()
            db_mod.save_config({"test": 1})  # должна вернуться, не висеть
            dt = time.monotonic() - t0
            assert 2.5 <= dt <= 5.0, f"ожидали ~3с таймаут, было {dt:.1f}с"
        finally:
            lock.release()

    def test_theme_cache_invalidation(self):
        import db as db_mod

        db_mod.set_theme("dark")
        assert db_mod.get_theme() == "dark"
        db_mod.set_theme("light")
        # после set_theme кеш инвалидируется — возвращается новое значение
        assert db_mod.get_theme() == "light"
        db_mod.set_theme("system")
        assert db_mod.get_theme() == "system"
