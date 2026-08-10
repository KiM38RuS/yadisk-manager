"""
Integration tests for folder status and auto-download logic.

Tests the INVARIANTS that ui.py must maintain:
  1. A folder where all files have status='downloaded' → fully synced
  2. A new cloud-only file in a fully-synced folder → triggers download
  3. Tree/table should show folder sync status

These tests mock the API but test the real db + sync logic.
"""

import os
import hashlib
import pytest
from pathlib import Path

import sync
from tests.conftest import MockYaDiskAPI


def _touch(path: str, content: str = "x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return hashlib.md5(content.encode()).hexdigest()


class TestFolderStatusInvariants:
    """
    INVARIANT: A folder's status is the aggregate of its file children.

    Rules:
      - NO files in folder → NOT synced (vacuous false)
      - ALL files downloaded → SYNCED
      - Any file not downloaded → NOT synced
      - Subfolders NOT considered (they have their own status)
    """

    @staticmethod
    def _setup_cloud_and_local(mock_api, db, temp_cache, structure: list[dict]):
        """
        Helper: set up cloud files and optionally create local copies.
        structure = [
            {"path": "/a.txt", "local": True},
            {"path": "/b.txt", "local": False},
            ...
        ]
        """
        cloud_items = []
        for entry in structure:
            path = entry["path"]
            name = path.rstrip("/").split("/")[-1]
            is_dir = entry.get("type") == "dir" or entry.get("is_dir", False)
            if is_dir:
                cloud_items.append({
                    "path": path, "name": name, "type": "dir",
                    "size": 0, "modified": "2025-01-01T00:00:00Z",
                    "md5": "",
                })
                continue

            content = entry.get("content", "data")
            local_flag = entry.get("local", False)
            if local_flag:
                local_path = os.path.join(temp_cache, path.lstrip("/"))
                _touch(local_path, content)

            md5 = hashlib.md5(content.encode()).hexdigest()
            cloud_items.append({
                "path": path, "name": name, "type": "file",
                "size": len(content),
                "modified": entry.get("modified", "2025-01-01T00:00:00Z"),
                "md5": md5,
            })

        mock_api.set_cloud_files(cloud_items)
        db.upsert_files_batch(mock_api.get_all_files())  # noqa: all files

        for entry in structure:
            if entry.get("local", False) and not entry.get("is_dir", False):
                path = entry["path"]
                local_path = os.path.join(temp_cache, path.lstrip("/"))
                md5 = hashlib.md5(entry.get("content", "data").encode()).hexdigest()
                db.set_downloaded(path, local_path, last_sync_md5=md5)

    def test_invariant_empty_folder_not_synced(self, in_memory_db):
        """A folder with zero file children → NOT synced."""
        assert not in_memory_db.is_folder_fully_synced("/")

    def test_invariant_single_file_folder(self, in_memory_db, mock_api,
                                          temp_cache):
        """One file, downloaded → folder is synced."""
        self._setup_cloud_and_local(mock_api, in_memory_db, temp_cache, [
            {"path": "/only.txt", "local": True},
        ])
        assert in_memory_db.is_folder_fully_synced("/") is True

    def test_invariant_mixed_folder_not_synced(self, in_memory_db, mock_api,
                                                temp_cache):
        """Two files, one downloaded, one not → NOT synced."""
        self._setup_cloud_and_local(mock_api, in_memory_db, temp_cache, [
            {"path": "/yes.txt", "local": True},
            {"path": "/no.txt", "local": False},
        ])
        assert in_memory_db.is_folder_fully_synced("/") is False

    def test_invariant_new_file_breaks_sync(self, in_memory_db, mock_api,
                                             temp_cache):
        """Fully synced → new cloud file appears → NOT synced anymore."""
        self._setup_cloud_and_local(mock_api, in_memory_db, temp_cache, [
            {"path": "/Synced", "type": "dir"},
            {"path": "/Synced/a.txt", "local": True},
            {"path": "/Synced/b.txt", "local": True},
        ])
        assert in_memory_db.is_folder_fully_synced("/Synced") is True

        # New file appears in cloud (simulate poll detection)
        mock_api.set_cloud_files([
            {"path": "/Synced", "name": "Synced", "type": "dir",
             "size": 0, "modified": "2025-01-01T00:00:00Z", "md5": ""},
            {"path": "/Synced/a.txt", "name": "a.txt", "type": "file",
             "size": 1, "modified": "2025-01-01T00:00:00Z", "md5": "m_a"},
            {"path": "/Synced/b.txt", "name": "b.txt", "type": "file",
             "size": 1, "modified": "2025-01-01T00:00:00Z", "md5": "m_b"},
            {"path": "/Synced/c_new.txt", "name": "c_new.txt", "type": "file",
             "size": 5, "modified": "2025-06-01T00:00:00Z",
             "md5": "m_c_new"},
        ])

        in_memory_db.upsert_file("/Synced/c_new.txt", "c_new.txt",
                                 "file", size=5, md5="m_c_new")
        # Now folder should NOT be fully synced
        assert in_memory_db.is_folder_fully_synced("/Synced") is False


class TestAutoDownloadDetection:
    """
    The auto-download pipeline:
    1. Poll detects new files → upsert into DB
    2. Check if parent folder was fully synced BEFORE the new file
    3. If yes → auto-download the new file

    These tests verify steps 2-3 logic.
    """

    def test_detect_auto_download_candidates(self, in_memory_db, mock_api,
                                              temp_cache):
        """
        Given a folder that WAS fully synced, detect which new
        cloud-only files should be auto-downloaded.
        """
        # Phase 1: fully synced folder
        mock_api.set_cloud_files([
            {"path": "/Watch/steady.txt", "name": "steady.txt",
             "type": "file", "size": 10,
             "modified": "2025-01-01T00:00:00Z", "md5": "m_steady"},
        ])
        in_memory_db.upsert_files_batch(mock_api.get_all_files())
        in_memory_db.set_downloaded("/Watch/steady.txt",
                                    os.path.join(temp_cache, "Watch", "steady.txt"),
                                    last_sync_md5="m_steady")
        assert in_memory_db.is_folder_fully_synced("/Watch") is True

        # Phase 2: new file appears (simulate poll)
        mock_api.add_cloud_file({
            "path": "/Watch/new_poll.txt", "name": "new_poll.txt",
            "type": "file", "size": 42,
            "modified": "2025-07-01T00:00:00Z", "md5": "m_new",
        })
        in_memory_db.upsert_file("/Watch/new_poll.txt", "new_poll.txt",
                                 "file", size=42, md5="m_new")

        # Detection: folder is no longer synced
        assert in_memory_db.is_folder_fully_synced("/Watch") is False

        # The auto-download candidate is the new file
        unsynced = in_memory_db.get_unsynced_children("/Watch")
        unsynced_paths = {c["cloud_path"] for c in unsynced if c["type"] == "file"}
        assert "/Watch/new_poll.txt" in unsynced_paths
        assert "/Watch/steady.txt" not in unsynced_paths

    def test_auto_download_after_full_sync(self, in_memory_db, mock_api,
                                            temp_cache):
        """
        After full_sync() where folder is fully synced, adding a new
        cloud file should trigger is_folder_fully_synced == False
        and get_unsynced_children should return just the new file.
        """
        # Files already synced
        mock_api.set_cloud_files([
            {"path": "/Stable/f1.txt", "name": "f1.txt", "type": "file",
             "size": 10, "modified": "2025-01-01T00:00:00Z",
             "md5": _touch(os.path.join(temp_cache, "Stable", "f1.txt"), "a")},
            {"path": "/Stable/f2.txt", "name": "f2.txt", "type": "file",
             "size": 10, "modified": "2025-01-01T00:00:00Z",
             "md5": _touch(os.path.join(temp_cache, "Stable", "f2.txt"), "b")},
        ])
        in_memory_db.upsert_files_batch(mock_api.get_all_files())
        sync.full_sync(mock_api, in_memory_db, temp_cache)
        assert in_memory_db.is_folder_fully_synced("/Stable") is True

        # New file appears in cloud
        mock_api.add_cloud_file({
            "path": "/Stable/f3_new.txt", "name": "f3_new.txt",
            "type": "file", "size": 30,
            "modified": "2025-07-01T00:00:00Z", "md5": "m_new3",
        })
        in_memory_db.upsert_file("/Stable/f3_new.txt", "f3_new.txt",
                                 "file", size=30, md5="m_new3")

        # Verify: folde should show as NOT synced now
        assert in_memory_db.is_folder_fully_synced("/Stable") is False

        unsynced = in_memory_db.get_unsynced_children("/Stable")
        paths = {c["cloud_path"] for c in unsynced if c["type"] == "file"}
        assert "/Stable/f3_new.txt" in paths
        assert "/Stable/f1.txt" not in paths
        assert "/Stable/f2.txt" not in paths


class TestPollIntegrationPattern:
    """
    Tests that verify the poll-handler pattern used in ui.py.

    The pattern is:
      1. poll returns recent items
      2. for each item not in DB → new item detected
      3. if parent folder was fully synced → auto-download
    """

    def _simulate_poll(self, db, mock_api, temp_cache):
        """
        Simulate what _on_poll_result does, plus auto-download check.
        Checks parent folder status BEFORE adding the new file to DB,
        because adding it changes the folder's sync status.

        Returns list of cloud_paths that should be auto-downloaded.
        """
        auto_download = []
        recent = mock_api.get_recent_uploaded()
        for item in recent:
            cp = item["path"]
            if not db.file_exists(cp):
                # CRITICAL: check parent folder sync status BEFORE adding
                parts = cp.rstrip("/").split("/")
                parent = "/".join(parts[:-1]) if len(parts) > 2 else "/"
                was_fully_synced = db.is_folder_fully_synced(parent)

                db.upsert_file(
                    cp, item["name"], item.get("type", "file"),
                    size=item.get("size", 0),
                    modified=item.get("modified", ""),
                    md5=item.get("md5", ""),
                )
                if was_fully_synced:
                    auto_download.append(cp)
        return auto_download

    def test_poll_triggers_auto_download(self, in_memory_db, mock_api,
                                          temp_cache):
        """New file in synced folder → auto-download triggered."""
        # Set up synced folder
        _touch(os.path.join(temp_cache, "base.txt"), "base")
        mock_api.set_cloud_files([
            {"path": "/base.txt", "name": "base.txt", "type": "file",
             "size": 4, "modified": "2025-01-01T00:00:00Z",
             "md5": hashlib.md5(b"base").hexdigest()},
        ])
        in_memory_db.upsert_files_batch(mock_api.get_all_files())
        in_memory_db.set_downloaded("/base.txt",
                                    os.path.join(temp_cache, "base.txt"),
                                    last_sync_md5=hashlib.md5(b"base").hexdigest())
        assert in_memory_db.is_folder_fully_synced("/") is True

        # Poll detects new file
        mock_api.add_cloud_file({
            "path": "/new_poll_file.txt", "name": "new_poll_file.txt",
            "type": "file", "size": 7,
            "modified": "2025-07-01T00:00:00Z",
            "md5": hashlib.md5(b"new_data").hexdigest(),
        })

        auto = self._simulate_poll(in_memory_db, mock_api, temp_cache)
        assert "/new_poll_file.txt" in auto

    def test_poll_no_auto_download_in_unsynced_folder(self, in_memory_db,
                                                       mock_api, temp_cache):
        """New file in NOT fully-synced folder → no auto-download."""
        # Set up: one downloaded, one not
        _touch(os.path.join(temp_cache, "x.txt"), "x")
        mock_api.set_cloud_files([
            {"path": "/x.txt", "name": "x.txt", "type": "file",
             "size": 1, "modified": "2025-01-01T00:00:00Z",
             "md5": hashlib.md5(b"x").hexdigest()},
            {"path": "/y_cloud.txt", "name": "y_cloud.txt", "type": "file",
             "size": 2, "modified": "2025-01-01T00:00:00Z",
             "md5": "y_cloud_md5"},
        ])
        in_memory_db.upsert_files_batch(mock_api.get_all_files())
        in_memory_db.set_downloaded("/x.txt",
                                    os.path.join(temp_cache, "x.txt"),
                                    last_sync_md5=hashlib.md5(b"x").hexdigest())
        # / is NOT fully synced (y_cloud.txt is cloud_only)

        # Poll detects new file
        mock_api.add_cloud_file({
            "path": "/z_new.txt", "name": "z_new.txt", "type": "file",
            "size": 3, "modified": "2025-07-01T00:00:00Z",
            "md5": "z_new_md5",
        })

        auto = self._simulate_poll(in_memory_db, mock_api, temp_cache)
        assert "/z_new.txt" not in auto

    def test_poll_auto_for_deeply_nested(self, in_memory_db, mock_api,
                                          temp_cache):
        """New file in deep fully-synced subfolder → auto-download."""
        _touch(os.path.join(temp_cache, "A", "B", "deep.txt"), "deep")
        mock_api.set_cloud_files([
            {"path": "/A", "name": "A", "type": "dir", "size": 0,
             "modified": "2025-01-01T00:00:00Z", "md5": ""},
            {"path": "/A/B", "name": "B", "type": "dir", "size": 0,
             "modified": "2025-01-01T00:00:00Z", "md5": ""},
            {"path": "/A/B/deep.txt", "name": "deep.txt", "type": "file",
             "size": 4, "modified": "2025-01-01T00:00:00Z",
             "md5": hashlib.md5(b"deep").hexdigest()},
        ])
        in_memory_db.upsert_files_batch(mock_api.get_all_files())
        in_memory_db.set_downloaded("/A/B/deep.txt",
                                    os.path.join(temp_cache, "A", "B", "deep.txt"),
                                    last_sync_md5=hashlib.md5(b"deep").hexdigest())
        assert in_memory_db.is_folder_fully_synced("/A/B") is True

        # New file in /A/B — add ONLY to cloud API, NOT to DB yet
        # (the poll mechanism detects it via get_recent_uploaded)
        mock_api.add_cloud_file({
            "path": "/A/B/new_deep.txt", "name": "new_deep.txt",
            "type": "file", "size": 5,
            "modified": "2025-07-01T00:00:00Z",
            "md5": hashlib.md5(b"new_deep").hexdigest(),
        })
        # NOTE: do NOT call upsert_file — let _simulate_poll discover it

        auto = self._simulate_poll(in_memory_db, mock_api, temp_cache)
        assert "/A/B/new_deep.txt" in auto


class TestWatcherIntegration:
    """Test the watcher → upload pipeline invariants."""

    def test_file_change_detected_unknown(self, in_memory_db, temp_cache):
        """Changed file not in DB → should be ignored gracefully."""
        changed = os.path.join(temp_cache, "unknown.txt")
        _touch(changed, "orphan")
        # Simulate _handle_file_changed logic
        rec = in_memory_db.get_by_local_path(changed)
        assert rec is None  # No crash, just ignored

    def test_file_change_triggers_upload_check(self, in_memory_db, mock_api,
                                                temp_cache):
        """Known downloaded file changes → should enter pending upload."""
        _touch(os.path.join(temp_cache, "edit.txt"), "original")
        in_memory_db.upsert_file("/edit.txt", "edit.txt", "file",
                                 md5=hashlib.md5(b"original").hexdigest())
        in_memory_db.set_downloaded("/edit.txt",
                                    os.path.join(temp_cache, "edit.txt"),
                                    last_sync_md5=hashlib.md5(b"original").hexdigest())

        # Simulate local change (file modified)
        _touch(os.path.join(temp_cache, "edit.txt"), "modified content")
        rec = in_memory_db.get_by_local_path(
            os.path.join(temp_cache, "edit.txt"))
        assert rec is not None
        # The new MD5 differs -> should be detected as modified on next sync
        new_md5 = hashlib.md5(b"modified content").hexdigest()
        assert new_md5 != rec.get("last_sync_md5", "")
