"""
Tests for sync.py — full sync, conflict resolution, edge cases.

Uses MockYaDiskAPI + temp cache directory so no real HTTP or
file I/O touches production data.
"""

import os
import hashlib
import pytest
from pathlib import Path

import sync
import disk_api
from tests.conftest import MockYaDiskAPI


def _touch(path: str, content: str = "data"):
    """Create a file with given content and return its MD5."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return hashlib.md5(content.encode()).hexdigest()


class TestFullSync:
    """Full sync with controlled cloud vs local states."""

    def test_empty_cache_dir(self, in_memory_db, mock_api, temp_cache):
        """Empty cache dir + empty cloud → nothing to do."""
        result = sync.full_sync(mock_api, in_memory_db, temp_cache)
        assert result["matched"] == 0
        assert result["uploaded"] == 0
        assert result["downloaded"] == 0
        assert result["moved"] == 0
        assert result["deleted"] == 0

    def test_all_matched(self, in_memory_db, mock_api, temp_cache):
        """All files matched between cloud and local."""
        mock_api.set_cloud_files([
            {"path": "/doc.txt", "name": "doc.txt", "type": "file",
             "size": 10, "modified": "2025-01-01T00:00:00Z",
             "md5": _touch(os.path.join(temp_cache, "doc.txt"), "hello")},
            {"path": "/data/stats.csv", "name": "stats.csv", "type": "file",
             "size": 20, "modified": "2025-01-01T00:00:00Z",
             "md5": _touch(os.path.join(temp_cache, "data", "stats.csv"), "1,2,3")},
        ])
        in_memory_db.upsert_files_batch(mock_api.get_all_files())
        result = sync.full_sync(mock_api, in_memory_db, temp_cache)
        assert result["matched"] >= 2
        # Both files should be marked downloaded
        assert in_memory_db.get_file("/doc.txt")["status"] == "downloaded"
        assert in_memory_db.get_file("/data/stats.csv")["status"] == "downloaded"

    def test_cloud_newer_download(self, in_memory_db, mock_api, temp_cache):
        """Cloud version is newer → download overwrites local."""
        import time as time_module
        local_path = os.path.join(temp_cache, "f.txt")
        local_md5 = _touch(local_path, "old content")
        # Set local mtime to be OLDER than cloud modified time
        old_mtime = 1717200000.0  # 2024-06-01
        os.utime(local_path, (old_mtime, old_mtime))
        mock_api.set_cloud_files([
            {"path": "/f.txt", "name": "f.txt", "type": "file",
             "size": 20, "modified": "2025-06-01T00:00:00Z",
             "md5": "different_md5_for_newer_version"},
        ])
        in_memory_db.upsert_files_batch(mock_api.get_all_files())
        # Mark local file as downloaded with old md5
        in_memory_db.set_downloaded("/f.txt", local_path,
                                    last_sync_md5=local_md5)
        result = sync.full_sync(mock_api, in_memory_db, temp_cache)
        assert result["downloaded"] >= 1
        # File should be overwritten by mock download
        rec = in_memory_db.get_file("/f.txt")
        assert rec["status"] == "downloaded"

    def test_local_newer_upload(self, in_memory_db, mock_api, temp_cache):
        """Local file is newer → upload to cloud."""
        local_md5 = _touch(os.path.join(temp_cache, "n.txt"), "local newer")
        mock_api.set_cloud_files([
            {"path": "/n.txt", "name": "n.txt", "type": "file",
             "size": 10, "modified": "2024-01-01T00:00:00Z",
             "md5": "old_cloud_md5"},
        ])
        in_memory_db.upsert_files_batch(mock_api.get_all_files())
        in_memory_db.set_downloaded("/n.txt",
                                    os.path.join(temp_cache, "n.txt"),
                                    last_sync_md5="old_cloud_md5")
        result = sync.full_sync(mock_api, in_memory_db, temp_cache)
        assert result["uploaded"] >= 1

    def test_new_local_file_uploaded(self, in_memory_db, mock_api, temp_cache):
        """Local file not in cloud → upload as new."""
        _touch(os.path.join(temp_cache, "new.txt"), "brand new")
        # Cloud is empty
        result = sync.full_sync(mock_api, in_memory_db, temp_cache)
        assert result["uploaded"] >= 1
        assert mock_api._cloud.get("/new.txt") is not None

    def test_file_deleted_from_cloud(self, in_memory_db, mock_api, temp_cache):
        """Downloaded file removed from cloud → delete local copy."""
        local_path = os.path.join(temp_cache, "gone.txt")
        _touch(local_path, "will be deleted")
        in_memory_db.upsert_file("/gone.txt", "gone.txt", "file",
                                 size=10, md5="m")
        in_memory_db.set_downloaded("/gone.txt", local_path,
                                    last_sync_md5="m")
        # Cloud is empty — file doesn't exist
        result = sync.full_sync(mock_api, in_memory_db, temp_cache)
        assert result["deleted"] >= 1
        assert not os.path.exists(local_path)
        assert not in_memory_db.file_exists("/gone.txt")


class TestFullSyncFolderStatus:
    """
    After full_sync(), folders should be correctly classified as
    fully synced or not.  This is THE critical invariant.
    """

    def test_after_sync_folder_is_fully_synced(self, in_memory_db, mock_api,
                                                temp_cache):
        """All cloud files exist locally with matching MD5 → folder synced."""
        mock_api.set_cloud_files([
            {"path": "/Docs", "name": "Docs", "type": "dir",
             "size": 0, "modified": "2025-01-01T00:00:00Z", "md5": ""},
            {"path": "/Docs/a.txt", "name": "a.txt", "type": "file",
             "size": 10, "modified": "2025-01-01T00:00:00Z",
             "md5": _touch(os.path.join(temp_cache, "Docs", "a.txt"), "aa")},
            {"path": "/Docs/b.txt", "name": "b.txt", "type": "file",
             "size": 10, "modified": "2025-01-01T00:00:00Z",
             "md5": _touch(os.path.join(temp_cache, "Docs", "b.txt"), "bb")},
        ])
        sync.full_sync(mock_api, in_memory_db, temp_cache)
        assert in_memory_db.is_folder_fully_synced("/Docs") is True

    def test_after_sync_partial_folder_not_synced(self, in_memory_db,
                                                   mock_api, temp_cache):
        """Only some files present locally → folder NOT synced."""
        mock_api.set_cloud_files([
            {"path": "/Docs/a.txt", "name": "a.txt", "type": "file",
             "size": 10, "modified": "2025-01-01T00:00:00Z",
             "md5": _touch(os.path.join(temp_cache, "Docs", "a.txt"), "aa")},
            {"path": "/Docs/b.txt", "name": "b.txt", "type": "file",
             "size": 10, "modified": "2025-01-01T00:00:00Z",
             "md5": "cloud_only_md5"},  # no local file
        ])
        in_memory_db.upsert_files_batch(mock_api.get_all_files())
        # Only a.txt is downloaded
        in_memory_db.set_downloaded(
            "/Docs/a.txt",
            os.path.join(temp_cache, "Docs", "a.txt"),
            last_sync_md5="aa_md5",
        )
        sync.full_sync(mock_api, in_memory_db, temp_cache)
        # b.txt couldn't be downloaded (mock creates empty file in download_file)
        # but the cloud's b.txt has md5=cloud_only_md5, after download the local
        # md5 will be different, so it WON'T be matched.
        # After sync, /Docs should still NOT be fully synced because
        # there's a mismatch.
        assert in_memory_db.is_folder_fully_synced("/Docs") is False


class TestAutoDownloadTrigger:
    """
    When a folder IS fully synced and a NEW file appears in the cloud,
    auto-download must detect it.

    These tests verify the *detection logic* that ui.py should call.
    """

    def test_new_file_in_fully_synced_folder_detected(self, in_memory_db,
                                                       mock_api):
        """
        Given a fully synced folder, detect that a new cloud-only file
        should be auto-downloaded.
        """
        # Set up: all files downloaded
        mock_api.set_cloud_files([
            {"path": "/Sync/some.txt", "name": "some.txt", "type": "file",
             "size": 100, "modified": "2025-01-01T00:00:00Z",
             "md5": "abc"},
        ])
        in_memory_db.upsert_files_batch(mock_api.get_all_files())
        in_memory_db.set_downloaded("/Sync/some.txt", "/c/Sync/some.txt",
                                    last_sync_md5="abc")
        assert in_memory_db.is_folder_fully_synced("/Sync") is True

        # Now a new file appears in the cloud (as if via poll)
        mock_api.add_cloud_file({
            "path": "/Sync/new_file.txt", "name": "new_file.txt",
            "type": "file", "size": 50,
            "modified": "2025-06-01T00:00:00Z", "md5": "new_md5",
        })
        in_memory_db.upsert_file("/Sync/new_file.txt", "new_file.txt",
                                 "file", size=50, md5="new_md5")

        # Detection: folder is still fully synced?
        # Actually after adding the new file as cloud_only, the folder
        # should NOT be fully synced anymore
        assert in_memory_db.is_folder_fully_synced("/Sync") is False

        # The new file should be in the unsynced list
        unsynced = in_memory_db.get_unsynced_children("/Sync")
        paths = {c["cloud_path"] for c in unsynced}
        assert "/Sync/new_file.txt" in paths

    def test_auto_download_candidates(self, in_memory_db, mock_api):
        """
        Given a fully synced folder, get_unsynced_children identifies
        exactly the files that need auto-download.
        """
        mock_api.set_cloud_files([
            {"path": "/Auto/a.txt", "name": "a.txt", "type": "file",
             "size": 10, "modified": "2025-01-01T00:00:00Z", "md5": "m1"},
            {"path": "/Auto/b.txt", "name": "b.txt", "type": "file",
             "size": 20, "modified": "2025-01-01T00:00:00Z", "md5": "m2"},
            {"path": "/Auto/sub/c.txt", "name": "c.txt", "type": "file",
             "size": 30, "modified": "2025-01-01T00:00:00Z", "md5": "m3"},
        ])
        in_memory_db.upsert_files_batch(mock_api.get_all_files())
        for f in ["/Auto/a.txt", "/Auto/b.txt", "/Auto/sub/c.txt"]:
            in_memory_db.set_downloaded(
                f, f"/c{f}", last_sync_md5=mock_api._cloud[f]["md5"])

        assert in_memory_db.is_folder_fully_synced("/Auto") is True

        # Add new file
        mock_api.add_cloud_file({
            "path": "/Auto/emerging.txt", "name": "emerging.txt",
            "type": "file", "size": 5,
            "modified": "2025-07-01T00:00:00Z", "md5": "new",
        })
        in_memory_db.upsert_file("/Auto/emerging.txt", "emerging.txt",
                                 "file", size=5, md5="new")

        # The new file should be detected
        unsynced = in_memory_db.get_unsynced_children("/Auto")
        unsynced_paths = {c["cloud_path"] for c in unsynced if c["type"] == "file"}
        assert "/Auto/emerging.txt" in unsynced_paths
        # Previously synced files should NOT appear
        assert "/Auto/a.txt" not in unsynced_paths
        assert "/Auto/b.txt" not in unsynced_paths


class TestConflictResolution:
    """Test the conflict detection logic from sync.py."""

    def test_match_no_conflict(self, in_memory_db, mock_api, temp_cache):
        """MD5 matches → no conflict, just match."""
        local_md5 = _touch(os.path.join(temp_cache, "ok.txt"), "same")
        mock_api.set_cloud_files([
            {"path": "/ok.txt", "name": "ok.txt", "type": "file",
             "size": 4, "modified": "2025-01-01T00:00:00Z", "md5": local_md5},
        ])
        in_memory_db.upsert_file("/ok.txt", "ok.txt", "file",
                                 md5=local_md5)
        in_memory_db.set_downloaded("/ok.txt",
                                    os.path.join(temp_cache, "ok.txt"),
                                    last_sync_md5=local_md5)
        result = sync.full_sync(mock_api, in_memory_db, temp_cache)
        assert result["matched"] >= 1

    def test_api_failure_graceful(self, in_memory_db, mock_api, temp_cache):
        """API failure during full_sync should not crash."""
        mock_api.fail_next(1)
        result = sync.full_sync(mock_api, in_memory_db, temp_cache)
        assert isinstance(result, dict)
        assert "matched" in result


class TestMigrateCache:
    """Tests for sync.migrate_cache()."""

    def test_migrate_to_new_dir(self, in_memory_db, mock_api, temp_cache):
        """Files should be moved and DB paths updated."""
        # Create source files
        _touch(os.path.join(temp_cache, "f1.txt"), "file1")
        _touch(os.path.join(temp_cache, "sub", "f2.txt"), "file2")

        in_memory_db.upsert_file("/f1.txt", "f1.txt", "file", md5="m1")
        in_memory_db.set_downloaded("/f1.txt",
                                    os.path.join(temp_cache, "f1.txt"),
                                    last_sync_md5="m1")
        in_memory_db.upsert_file("/sub/f2.txt", "f2.txt", "file", md5="m2")
        in_memory_db.set_downloaded("/sub/f2.txt",
                                    os.path.join(temp_cache, "sub", "f2.txt"),
                                    last_sync_md5="m2")

        new_dir = temp_cache + "_new"
        count = sync.migrate_cache(temp_cache, new_dir, in_memory_db)

        assert count == 2
        assert os.path.exists(os.path.join(new_dir, "f1.txt"))
        assert os.path.exists(os.path.join(new_dir, "sub", "f2.txt"))
        # DB paths updated
        assert in_memory_db.get_file("/f1.txt")["local_path"] == \
            os.path.join(new_dir, "f1.txt")
        # Source dir no longer has the files (moved)
        assert not os.path.exists(os.path.join(temp_cache, "f1.txt"))

    def test_migrate_same_dir(self, in_memory_db, temp_cache):
        """Migrating to same dir → no-op."""
        count = sync.migrate_cache(temp_cache, temp_cache, in_memory_db)
        assert count == 0
